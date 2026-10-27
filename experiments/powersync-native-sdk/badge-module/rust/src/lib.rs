#![no_std]
extern crate alloc;

use alloc::{
    alloc::{GlobalAlloc, Layout},
    boxed::Box,
    string::{String, ToString},
    sync::{Arc, Weak},
    vec::Vec,
};
use async_trait::async_trait;
use base64::{Engine as _, engine::general_purpose::STANDARD};
use bytes::Bytes;
use core::{
    ffi::c_void,
    future::Future,
    mem,
    pin::Pin,
    ptr,
    sync::atomic::{AtomicBool, Ordering},
    task::{Context as TaskContext, Poll, Waker},
    time::Duration,
};
use embassy_executor::raw::Executor;
use event_listener::Event;
use futures_lite::{FutureExt, Stream, future::Boxed};
use powersync::{
    BackendConnector, CheckpointMode, ConnectionPool, LeasedConnection, PowerSyncCredentials,
    PowerSyncDatabase, RawSqliteConnection, StreamSubscription, SyncOptions, UpdateType,
    env::{
        PowerSyncEnvironment,
        embassy::{EmbassyRuntime, EmbassyTaskPool},
    },
    error::PowerSyncError,
    http::{HttpClient, Request, Response, ResponseBody},
};
use powersync_sqlite_nostd::{self as sqlite, ResultCode};
use serde_json::{Value, json};
use spin::Mutex;

unsafe extern "C" {
    fn psb_runtime_context() -> *mut c_void;
    fn psb_set_runtime_context(ptr: *mut c_void);
    fn psb_trace(stage: u32);
    fn psb_panic_report(file: *const u8, length: usize, line: u32);
    fn psb_panic_abort() -> !;
}

struct Allocator;
unsafe impl GlobalAlloc for Allocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        if layout.align() <= 64 {
            return sqlite::malloc(layout.size().max(1));
        }
        let Some(size) = layout
            .size()
            .checked_add(layout.align())
            .and_then(|n| n.checked_add(mem::size_of::<usize>()))
        else {
            return ptr::null_mut();
        };
        let raw = sqlite::malloc(size);
        if raw.is_null() {
            return raw;
        }
        let aligned = ((raw as usize + mem::size_of::<usize>() + layout.align() - 1)
            & !(layout.align() - 1)) as *mut u8;
        unsafe {
            aligned.cast::<*mut u8>().sub(1).write(raw);
        }
        aligned
    }
    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        let raw = if layout.align() > 64 {
            unsafe { ptr.cast::<*mut u8>().sub(1).read() }
        } else {
            ptr
        };
        sqlite::free(raw.cast());
    }
}
#[global_allocator]
static ALLOCATOR: Allocator = Allocator;
#[panic_handler]
fn panic(info: &core::panic::PanicInfo<'_>) -> ! {
    if let Some(location) = info.location() {
        unsafe {
            psb_panic_report(
                location.file().as_ptr(),
                location.file().len(),
                location.line(),
            );
        }
    } else {
        unsafe {
            psb_panic_report(ptr::null(), 0, 0);
        }
    }
    unsafe { psb_panic_abort() }
}

fn trace(stage: u32) {
    unsafe {
        psb_trace(stage);
    }
}

struct Timer {
    id: u32,
    deadline: Duration,
    waker: Waker,
}
struct Clock {
    now: Duration,
    next: u32,
    timers: Vec<Timer>,
}
struct RuntimeState {
    pending: AtomicBool,
    clock: Mutex<Clock>,
}
struct Engine {
    state: &'static RuntimeState,
    executor: &'static Executor,
    tasks: &'static EmbassyTaskPool<8>,
}

fn engine() -> &'static Engine {
    unsafe {
        trace(110);
        let existing = psb_runtime_context();
        if !existing.is_null() {
            return &*existing.cast::<Engine>();
        }
        trace(111);
        let state = Box::leak(Box::new(RuntimeState {
            pending: AtomicBool::new(false),
            clock: Mutex::new(Clock {
                now: Duration::ZERO,
                next: 0,
                timers: Vec::new(),
            }),
        }));
        trace(112);
        let executor = Box::leak(Box::new(Executor::new(
            (state as *const RuntimeState).cast_mut().cast(),
        )));
        trace(113);
        let tasks = Box::leak(Box::new(EmbassyTaskPool::new()));
        trace(114);
        let runtime = Box::leak(Box::new(Engine {
            state,
            executor,
            tasks,
        }));
        psb_set_runtime_context((runtime as *mut Engine).cast());
        trace(115);
        runtime
    }
}

#[unsafe(export_name = "__pender")]
fn pender(context: *mut ()) {
    unsafe { &*context.cast::<RuntimeState>() }
        .pending
        .store(true, Ordering::Release);
}

struct Delay {
    state: &'static RuntimeState,
    id: u32,
    deadline: Duration,
}
fn delay(duration: Duration) -> Boxed<()> {
    let state = engine().state;
    let mut clock = state.clock.lock();
    let id = clock.next;
    clock.next = clock.next.wrapping_add(1);
    let deadline = clock.now + duration;
    Box::pin(Delay {
        state,
        id,
        deadline,
    })
}
impl Future for Delay {
    type Output = ();
    fn poll(self: Pin<&mut Self>, cx: &mut TaskContext<'_>) -> Poll<()> {
        let mut clock = self.state.clock.lock();
        if clock.now >= self.deadline {
            return Poll::Ready(());
        }
        if let Some(timer) = clock.timers.iter_mut().find(|t| t.id == self.id) {
            timer.waker.clone_from(cx.waker());
        } else {
            clock.timers.push(Timer {
                id: self.id,
                deadline: self.deadline,
                waker: cx.waker().clone(),
            });
        }
        Poll::Pending
    }
}
impl Drop for Delay {
    fn drop(&mut self) {
        let mut c = self.state.clock.lock();
        if let Some(i) = c.timers.iter().position(|t| t.id == self.id) {
            c.timers.swap_remove(i);
        }
    }
}
impl Engine {
    fn advance(&self, elapsed: u32) {
        let mut clock = self.state.clock.lock();
        clock.now += Duration::from_millis(elapsed.min(60000) as u64);
        let now = clock.now;
        let mut ready = Vec::new();
        let mut i = 0;
        while i < clock.timers.len() {
            if clock.timers[i].deadline <= now {
                ready.push(clock.timers.swap_remove(i).waker);
            } else {
                i += 1;
            }
        }
        drop(clock);
        for wake in ready {
            wake.wake();
        }
    }
    fn tick(&self) {
        if self.state.pending.swap(false, Ordering::AcqRel) {
            unsafe {
                self.executor.poll();
            }
        }
    }
}

struct ExchangeState {
    headers: Option<(u16, Option<String>)>,
    chunk: Option<Bytes>,
    line_bytes: usize,
    eof: bool,
    error: bool,
    waker: Option<Waker>,
    active: bool,
}
struct Exchange {
    id: u32,
    state: Mutex<ExchangeState>,
    changed: Event,
}
struct Credentials {
    version: u32,
    token: Option<String>,
    error: Option<ResultCode>,
    pending: bool,
    invalidate: bool,
}
#[derive(Clone, Copy)]
enum UploadResult {
    Success,
    Rejected,
    Retry,
}
struct UploadState {
    result: Option<UploadResult>,
    active: bool,
}
struct UploadExchange {
    id: u32,
    request: Value,
    state: Mutex<UploadState>,
    changed: Event,
}
struct Shared {
    endpoint: String,
    requests: Mutex<Vec<Value>>,
    close: Mutex<Vec<u32>>,
    exchanges: Mutex<Vec<Arc<Exchange>>>,
    next: Mutex<u32>,
    credentials: Mutex<Credentials>,
    credentials_changed: Event,
    upload: Mutex<Option<Arc<UploadExchange>>>,
    upload_close: Mutex<Vec<u32>>,
    next_upload: Mutex<u32>,
}
#[derive(Clone)]
struct Connector {
    shared: Arc<Shared>,
    // The SDK owns its connector while connected. A strong database reference
    // here would form a cycle through the coordinator's SyncOptions.
    db: Weak<PowerSyncDatabase>,
}
#[derive(Debug)]
struct InvalidUpload(&'static str);
impl core::fmt::Display for InvalidUpload {
    fn fmt(&self, out: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        out.write_str(self.0)
    }
}
impl core::error::Error for InvalidUpload {}
fn invalid_upload(message: &'static str) -> PowerSyncError {
    PowerSyncError::upload_error(InvalidUpload(message))
}
#[async_trait]
impl BackendConnector for Connector {
    async fn fetch_credentials(&self) -> Result<PowerSyncCredentials, PowerSyncError> {
        let version = {
            let mut c = self.shared.credentials.lock();
            c.pending = true;
            c.version
        };
        loop {
            let changed = self.shared.credentials_changed.listen();
            {
                let c = self.shared.credentials.lock();
                if c.version != version {
                    if let Some(error) = c.error {
                        return Err(error.into());
                    }
                    return c
                        .token
                        .as_ref()
                        .map(|t| PowerSyncCredentials {
                            endpoint: self.shared.endpoint.clone(),
                            token: t.clone(),
                        })
                        .ok_or_else(|| ResultCode::AUTH.into());
                }
            }
            changed.await;
        }
    }
    async fn upload_data(&self) -> Result<(), PowerSyncError> {
        let db = self.db.upgrade().ok_or(ResultCode::ABORT)?;
        let Some(transaction) = db.next_crud_transaction().await? else {
            return Ok(());
        };
        // The game admits one immutable move per explicit transaction. Never
        // accidentally acknowledge unrelated or malformed local writes.
        if transaction.crud.len() != 1 {
            return Err(invalid_upload("Expected one queued badge move"));
        }
        let entry = &transaction.crud[0];
        if entry.table != "_badge_move_outbox" || !matches!(entry.update_type, UpdateType::Put) {
            return Err(invalid_upload("Unexpected badge upload table or operation"));
        }
        let data = entry
            .data
            .as_ref()
            .ok_or_else(|| invalid_upload("Missing badge move data"))?;
        let room = data
            .get("room_id")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_upload("Missing move room"))?;
        let orientation = data
            .get("orientation")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_upload("Missing move orientation"))?;
        let row = data
            .get("row_i")
            .and_then(Value::as_i64)
            .ok_or_else(|| invalid_upload("Missing move row"))?;
        let col = data
            .get("col_i")
            .and_then(Value::as_i64)
            .ok_or_else(|| invalid_upload("Missing move column"))?;
        if !is_uuid(&entry.id)
            || !is_uuid(room)
            || !match orientation {
                "h" => (0..=4).contains(&row) && (0..5).contains(&col),
                "v" => (0..4).contains(&row) && (0..=5).contains(&col),
                _ => false,
            }
        {
            return Err(invalid_upload("Invalid queued badge move"));
        }
        let id = {
            let mut next = self.shared.next_upload.lock();
            *next = next.wrapping_add(1);
            *next
        };
        let upload = Arc::new(UploadExchange {
            id,
            request: json!({"id":id,"entry_id":entry.client_id,"move":{"id":entry.id,"room_id":room,"orientation":orientation,"row":row,"col":col}}),
            state: Mutex::new(UploadState {
                result: None,
                active: true,
            }),
            changed: Event::new(),
        });
        {
            let mut active = self.shared.upload.lock();
            if active.as_ref().is_some_and(|u| u.state.lock().active) {
                return Err(ResultCode::BUSY.into());
            }
            *active = Some(upload.clone());
        }
        let _guard = UploadGuard {
            shared: self.shared.clone(),
            upload: upload.clone(),
        };
        let result = loop {
            let changed = upload.changed.listen();
            if let Some(result) = upload.state.lock().result {
                break result;
            }
            changed.await;
        };
        match result {
            // These acknowledge transport processing, not canonical board
            // publication. Python retains its exact pending UUID until full
            // sync confirms it, or separately persists a permanent rejection.
            UploadResult::Success | UploadResult::Rejected => transaction.complete().await,
            UploadResult::Retry => Err(ResultCode::IOERR.into()),
        }
    }
}
fn is_uuid(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(i, b)| {
            if matches!(i, 8 | 13 | 18 | 23) {
                b == b'-'
            } else {
                b.is_ascii_hexdigit()
            }
        })
}
struct UploadGuard {
    shared: Arc<Shared>,
    upload: Arc<UploadExchange>,
}
impl Drop for UploadGuard {
    fn drop(&mut self) {
        let mut state = self.upload.state.lock();
        if state.active {
            state.active = false;
            drop(state);
            self.shared.upload_close.lock().push(self.upload.id);
        }
    }
}
#[derive(Clone)]
struct Http(Arc<Shared>);
#[async_trait]
impl HttpClient for Http {
    async fn send(&self, request: Request) -> Result<Response, PowerSyncError> {
        let id = {
            let mut n = self.0.next.lock();
            *n = n.wrapping_add(1);
            *n
        };
        let exchange = Arc::new(Exchange {
            id,
            state: Mutex::new(ExchangeState {
                headers: None,
                chunk: None,
                line_bytes: 0,
                eof: false,
                error: false,
                waker: None,
                active: true,
            }),
            changed: Event::new(),
        });
        {
            let mut exchanges = self.0.exchanges.lock();
            exchanges.retain(|x| x.state.lock().active);
            if exchanges.len() >= 4 {
                return Err(ResultCode::BUSY.into());
            }
            exchanges.push(exchange.clone());
        }
        let streaming = request.url.path().ends_with("/sync/stream");
        let headers: Vec<Value> = request
            .headers
            .into_iter()
            .map(|(k, v)| {
                json!([
                    k,
                    if streaming && k.eq_ignore_ascii_case("accept") {
                        "application/x-ndjson"
                    } else {
                        v.as_ref()
                    }
                ])
            })
            .collect();
        let body = request
            .body
            .map(|b| String::from_utf8(b))
            .transpose()
            .map_err(|_| PowerSyncError::from(ResultCode::MISUSE))?;
        self.0.requests.lock().push(json!({"id":id,"method":request.method,"url":request.url.as_str(),"headers":headers,"body":body}));
        let mut guard = ExchangeGuard {
            shared: self.0.clone(),
            exchange: exchange.clone(),
            armed: true,
        };
        let (status, content_type) = loop {
            let changed = exchange.changed.listen();
            {
                let mut state = exchange.state.lock();
                if state.error {
                    return Err(ResultCode::IOERR.into());
                }
                if let Some(headers) = state.headers.take() {
                    break headers;
                }
            }
            changed.await;
        };
        guard.armed = false;
        Ok(Response {
            status,
            content_type,
            body: ResponseBody {
                reader: Box::pin(Body {
                    shared: self.0.clone(),
                    exchange,
                }),
                length: None,
            },
        })
    }
}
struct ExchangeGuard {
    shared: Arc<Shared>,
    exchange: Arc<Exchange>,
    armed: bool,
}
impl Drop for ExchangeGuard {
    fn drop(&mut self) {
        if self.armed {
            close_exchange(&self.shared, &self.exchange);
        }
    }
}
fn close_exchange(shared: &Shared, exchange: &Exchange) {
    let mut s = exchange.state.lock();
    if s.active {
        s.active = false;
        drop(s);
        shared.close.lock().push(exchange.id);
    }
}
struct Body {
    shared: Arc<Shared>,
    exchange: Arc<Exchange>,
}
impl Stream for Body {
    type Item = Result<Bytes, PowerSyncError>;
    fn poll_next(self: Pin<&mut Self>, cx: &mut TaskContext<'_>) -> Poll<Option<Self::Item>> {
        let mut s = self.exchange.state.lock();
        if s.error {
            s.error = false;
            s.eof = true;
            return Poll::Ready(Some(Err(ResultCode::IOERR.into())));
        }
        if let Some(chunk) = s.chunk.take() {
            return Poll::Ready(Some(Ok(chunk)));
        }
        if s.eof {
            return Poll::Ready(None);
        }
        s.waker = Some(cx.waker().clone());
        Poll::Pending
    }
}
impl Drop for Body {
    fn drop(&mut self) {
        close_exchange(&self.shared, &self.exchange);
    }
}

struct Context {
    db: Arc<PowerSyncDatabase>,
    pool: ConnectionPool,
    shared: Arc<Shared>,
    runtime: &'static Engine,
    starting: Option<Boxed<Result<StreamSubscription, PowerSyncError>>>,
    subscription: Option<StreamSubscription>,
    stopping: Option<Boxed<()>>,
    stopped: bool,
    barrier: Option<Boxed<Result<(), String>>>,
    barrier_ready: bool,
    // The SDK owns commit notifications, including ordinary and raw tables.
    // Foreground polling only exports coalesced invalidations to Python.
    watches: Vec<(u32, Pin<Box<dyn Stream<Item = ()> + Send>>)>,
    error: String,
}

fn text<'a>(p: *const u8, n: usize) -> Option<&'a str> {
    if p.is_null() || n > 32768 {
        return None;
    }
    core::str::from_utf8(unsafe { core::slice::from_raw_parts(p, n) }).ok()
}
fn poll_once<T>(future: &mut Pin<Box<dyn Future<Output = T> + Send>>) -> Poll<T> {
    future
        .as_mut()
        .poll(&mut TaskContext::from_waker(Waker::noop()))
}

#[repr(C)]
pub struct PsbBuffer {
    pub ptr: *mut u8,
    pub len: usize,
    pub capacity: usize,
}
impl PsbBuffer {
    fn from_vec(mut v: Vec<u8>) -> Self {
        let b = Self {
            ptr: v.as_mut_ptr(),
            len: v.len(),
            capacity: v.capacity(),
        };
        mem::forget(v);
        b
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn psb_start(
    db: *mut c_void,
    schema: *const u8,
    schema_len: usize,
    endpoint: *const u8,
    endpoint_len: usize,
    room: *const u8,
    room_len: usize,
) -> *mut c_void {
    trace(101);
    let Some(schema) = text(schema, schema_len) else {
        return ptr::null_mut();
    };
    trace(102);
    let Ok(schema) = serde_json::value::RawValue::from_string(String::from(schema)) else {
        return ptr::null_mut();
    };
    let Some(endpoint) = text(endpoint, endpoint_len) else {
        return ptr::null_mut();
    };
    let Some(room) = text(room, room_len) else {
        return ptr::null_mut();
    };
    if db.is_null() || !endpoint.starts_with("https://") || room.len() != 36 {
        return ptr::null_mut();
    }
    trace(103);
    let runtime = engine();
    trace(120);
    let shared = Arc::new(Shared {
        endpoint: String::from(endpoint),
        requests: Mutex::new(Vec::new()),
        close: Mutex::new(Vec::new()),
        exchanges: Mutex::new(Vec::new()),
        next: Mutex::new(0),
        credentials: Mutex::new(Credentials {
            version: 0,
            token: None,
            error: None,
            pending: false,
            invalidate: false,
        }),
        credentials_changed: Event::new(),
        upload: Mutex::new(None),
        upload_close: Mutex::new(Vec::new()),
        next_upload: Mutex::new(0),
    });
    trace(130);
    let borrowed = unsafe { RawSqliteConnection::from_handle_borrowed(db.cast()) };
    let pool = ConnectionPool::single_connection(borrowed);
    trace(140);
    let db = Arc::new(PowerSyncDatabase::new(
        PowerSyncEnvironment::custom(
            Http(shared.clone()),
            pool.clone(),
            EmbassyRuntime::new(runtime.executor.spawner().make_send(), runtime.tasks, delay),
        ),
        schema.as_ref(),
    ));
    trace(141);
    let begin_db = db.clone();
    let connector = Connector {
        shared: shared.clone(),
        db: Arc::downgrade(&db),
    };
    let room = String::from(room);
    trace(150);
    let starting = async move {
        let subscription = begin_db
            .sync_stream("badge_game_v2", Some(&json!({"room_id":room})))
            .subscribe()
            .await?;
        let mut options = SyncOptions::new(connector);
        options.set_include_default_streams(false);
        options.with_checkpoint_mode(CheckpointMode::Requests(Default::default()));
        begin_db.connect(options).await;
        Ok(subscription)
    }
    .boxed();
    trace(151);
    let result = Box::into_raw(Box::new(Context {
        db,
        pool,
        shared,
        runtime,
        starting: Some(starting),
        subscription: None,
        stopping: None,
        stopped: false,
        barrier: None,
        barrier_ready: false,
        watches: Vec::new(),
        error: String::new(),
    }))
    .cast();
    trace(160);
    result
}

fn service(c: &mut Context, elapsed: u32, budget: u32) {
    c.runtime.advance(elapsed);
    for _ in 0..budget.clamp(1, 6) {
        if let Some(start) = c.starting.as_mut() {
            if let Poll::Ready(result) = poll_once(start) {
                c.starting = None;
                match result {
                    Ok(subscription) => c.subscription = Some(subscription),
                    Err(e) => c.error = e.to_string(),
                }
            }
        }
        if let Some(barrier) = c.barrier.as_mut() {
            if let Poll::Ready(result) = poll_once(barrier) {
                c.barrier = None;
                match result {
                    Ok(()) => c.barrier_ready = true,
                    Err(e) => c.error = e,
                }
            }
        }
        if let Some(stop) = c.stopping.as_mut() {
            if poll_once(stop).is_ready() {
                c.stopping = None;
                c.stopped = true;
                c.subscription = None;
            }
        }
        c.runtime.tick();
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn psb_poll(context: *mut c_void, elapsed: u32, budget: u32) -> PsbBuffer {
    let c = unsafe { &mut *context.cast::<Context>() };
    service(c, elapsed, budget);
    let mut changed_watches = Vec::new();
    c.watches.retain_mut(|(id, stream)| {
        match stream
            .as_mut()
            .poll_next(&mut TaskContext::from_waker(Waker::noop()))
        {
            Poll::Ready(Some(())) => {
                changed_watches.push(*id);
                true
            }
            Poll::Ready(None) => false,
            Poll::Pending => true,
        }
    });
    let status = c.db.status();
    let requests = mem::take(&mut *c.shared.requests.lock());
    let close = mem::take(&mut *c.shared.close.lock());
    let upload = c
        .shared
        .upload
        .lock()
        .as_ref()
        .and_then(|upload| upload.state.lock().active.then(|| upload.request.clone()));
    let upload_close = mem::take(&mut *c.shared.upload_close.lock());
    let (needed, invalidate) = {
        let mut v = c.shared.credentials.lock();
        let r = (v.pending, v.invalidate);
        v.invalidate = false;
        r
    };
    let can_feed: Vec<u32> = c
        .shared
        .exchanges
        .lock()
        .iter()
        .filter_map(|e| {
            let s = e.state.lock();
            (s.active && s.headers.is_none() && s.chunk.is_none() && !s.eof && !s.error)
                .then_some(e.id)
        })
        .collect();
    let error = if !c.error.is_empty() {
        c.error.clone()
    } else {
        status
            .any_error()
            .map(|e| e.to_string())
            .unwrap_or_default()
    };
    let value = json!({"requests":requests,"close":close,"credentials_needed":needed,"invalidate_credentials":invalidate,
        "connected":status.is_connected(),"connecting":status.is_connecting(),"downloading":status.is_downloading(),"synced":status.has_synced(),
        "generation":status.full_generation(),"full_generation":status.full_generation(),"applied_checkpoint_request_id":status.applied_checkpoint_request_id().map(|x|x.to_string()),
        "barrier_pending":c.barrier.is_some(),"barrier_ready":c.barrier_ready,"error":error,"stopped":c.stopped,"can_feed":can_feed,
        "upload":upload,"upload_close":upload_close,"uploading":status.is_uploading(),"upload_error":status.upload_error().is_some(),
        "changed_watches":changed_watches});
    PsbBuffer::from_vec(serde_json::to_vec(&value).unwrap())
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn psb_command(context: *mut c_void, p: *const u8, n: usize) -> i32 {
    let c = unsafe { &mut *context.cast::<Context>() };
    let Some(raw) = text(p, n) else {
        return -1;
    };
    let Ok(value) = serde_json::from_str::<Value>(raw) else {
        return -1;
    };
    let op = value["op"].as_str().unwrap_or("");
    if op == "watch" || op == "unwatch" {
        let Some(id) = value["id"].as_u64().filter(|id| *id > 0 && *id <= u32::MAX as u64) else {
            return -1;
        };
        let id = id as u32;
        if op == "unwatch" {
            c.watches.retain(|(existing, _)| *existing != id);
            return 0;
        }
        if c.stopped || c.stopping.is_some() || c.watches.len() >= 4
            || c.watches.iter().any(|(existing, _)| *existing == id)
        {
            return -1;
        }
        let Some(tables) = value["tables"].as_array().filter(|tables| !tables.is_empty() && tables.len() <= 4) else {
            return -1;
        };
        let mut names = Vec::new();
        for table in tables {
            let Some(name) = table.as_str().filter(|name| {
                !name.is_empty() && name.len() <= 64
                    && name.bytes().enumerate().all(|(i, byte)| {
                        byte == b'_' || byte.is_ascii_alphabetic() || (i > 0 && byte.is_ascii_digit())
                    })
            }) else {
                return -1;
            };
            if names.iter().any(|existing| existing == name) {
                return -1;
            }
            names.push(String::from(name));
        }
        c.watches.push((id, Box::pin(c.db.watch_tables(true, names))));
        return 0;
    }
    if op == "credentials" || op == "credentials_error" {
        let mut creds = c.shared.credentials.lock();
        creds.version = creds.version.wrapping_add(1);
        creds.pending = false;
        creds.token = value["token"].as_str().map(String::from);
        creds.error = (op == "credentials_error").then_some(ResultCode::IOERR);
        drop(creds);
        c.shared.credentials_changed.notify(usize::MAX);
        return 0;
    }
    if op == "checkpoint" {
        if c.barrier.is_none() && !c.stopped {
            c.barrier_ready = false;
            let db = c.db.clone();
            c.barrier = Some(
                async move {
                    db.request_checkpoint()
                        .await
                        .map_err(|e| e.to_string())?
                        .wait_for_sync()
                        .await
                        .map_err(|e| e.to_string())
                }
                .boxed(),
            );
        }
        return 0;
    }
    if op == "upload" {
        let Some(id) = value["id"].as_u64() else {
            return -1;
        };
        let Some(result) = (match value["result"].as_str() {
            Some("success") => Some(UploadResult::Success),
            Some("rejected") => Some(UploadResult::Rejected),
            Some("retry") => Some(UploadResult::Retry),
            _ => None,
        }) else {
            return -1;
        };
        let upload = c.shared.upload.lock().clone();
        if let Some(upload) = upload.filter(|u| u.id as u64 == id) {
            let mut state = upload.state.lock();
            if state.active && state.result.is_none() {
                state.result = Some(result);
                drop(state);
                upload.changed.notify(usize::MAX);
            }
        }
        return 0;
    }
    let Some(id) = value["id"].as_u64() else {
        return -1;
    };
    let exchange = c
        .shared
        .exchanges
        .lock()
        .iter()
        .find(|e| e.id == id as u32)
        .cloned();
    let Some(exchange) = exchange else {
        return 0;
    };
    let mut state = exchange.state.lock();
    if !state.active {
        return 0;
    }
    match op {
        "headers" => {
            let Some(code) = value["status"].as_u64() else {
                return -1;
            };
            state.headers = Some((
                code as u16,
                value["content_type"].as_str().map(String::from),
            ));
            if code == 401 {
                c.shared.credentials.lock().invalidate = true;
            }
        }
        "chunk" => {
            if state.chunk.is_some() {
                return 1;
            }
            let Some(encoded) = value["data_b64"].as_str() else {
                return -1;
            };
            if encoded.len() > 2732 {
                return -1;
            }
            let Ok(data) = STANDARD.decode(encoded) else {
                return -1;
            };
            if data.len() > 2048 {
                return -1;
            }
            // Candidate transport accepts bounded NDJSON records. The platform
            // enforces this before the SDK's whole-record splitter allocates.
            for byte in &data {
                if *byte == b'\n' {
                    state.line_bytes = 0;
                } else {
                    state.line_bytes += 1;
                    if state.line_bytes > 65536 {
                        state.error = true;
                        c.error = String::from("PowerSync record exceeds 64 KiB");
                        break;
                    }
                }
            }
            if !state.error {
                state.chunk = Some(Bytes::from(data));
            }
        }
        "end" => state.eof = true,
        "error" => state.error = true,
        _ => return -1,
    }
    let waker = state.waker.take();
    drop(state);
    if let Some(waker) = waker {
        waker.wake();
    }
    exchange.changed.notify(usize::MAX);
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn psb_buffer_free(buffer: PsbBuffer) {
    if buffer.capacity != 0 {
        drop(unsafe { Vec::from_raw_parts(buffer.ptr, buffer.len, buffer.capacity) });
    }
}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn psb_stop(context: *mut c_void) -> i32 {
    let c = unsafe { &mut *context.cast::<Context>() };
    if c.stopped {
        return 1;
    }
    if c.stopping.is_none() {
        c.watches.clear();
        c.starting = None;
        c.barrier = None;
        c.barrier_ready = false;
        let db = c.db.clone();
        c.stopping = Some(
            async move {
                db.disconnect().await;
            }
            .boxed(),
        );
    }
    service(c, 0, 6);
    c.stopped as i32
}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn psb_destroy(context: *mut c_void) -> i32 {
    let c = unsafe { &mut *context.cast::<Context>() };
    if !c.stopped {
        return 0;
    }
    for _ in 0..16 {
        c.runtime.tick();
    }
    drop(unsafe { Box::from_raw(context.cast::<Context>()) });
    1
}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn psb_sql_enter(context: *mut c_void) -> *mut c_void {
    let c = unsafe { &mut *context.cast::<Context>() };
    let mut future = core::pin::pin!(c.pool.writer());
    match future
        .as_mut()
        .poll(&mut TaskContext::from_waker(Waker::noop()))
    {
        Poll::Ready(lease) => Box::into_raw(Box::new(lease)).cast(),
        Poll::Pending => ptr::null_mut(),
    }
}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn psb_sql_leave(lease: *mut c_void) {
    if !lease.is_null() {
        drop(unsafe { Box::from_raw(lease.cast::<LeasedConnection>()) });
    }
}
