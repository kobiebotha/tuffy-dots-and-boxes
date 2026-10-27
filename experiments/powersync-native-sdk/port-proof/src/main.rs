//! Actual no_std SDK port using Embassy foreground scheduling and constrained SQLite.
//! No live network, credentials, firmware, or application database are used.
use std::{alloc::{GlobalAlloc, Layout, System}, future::Future,
    pin::Pin, sync::{Arc, Mutex, atomic::{AtomicBool, AtomicUsize, Ordering}},
    task::{Context, Poll, Waker}, time::{Duration, Instant}};
use async_trait::async_trait;
use bytes::Bytes;
use embassy_executor::raw::Executor;
use futures_lite::{Stream, future::Boxed};
use powersync::{BackendConnector, ConnectionPool, PowerSyncCredentials, PowerSyncDatabase,
    RawSqliteConnection, SqliteConnection, SyncOptions, env::{PowerSyncEnvironment,
        embassy::{EmbassyRuntime, EmbassyTaskPool}},
    error::PowerSyncError, http::{HttpClient, Request, Response, ResponseBody}};
use powersync_sqlite_nostd::{bindings as sql, ResultCode};
use serde_json::{Value, json};

struct CountingAllocator;
static LIVE: AtomicUsize = AtomicUsize::new(0);
static PEAK: AtomicUsize = AtomicUsize::new(0);
static ROUTE_SQLITE: AtomicBool = AtomicBool::new(false);
unsafe impl GlobalAlloc for CountingAllocator {
    unsafe fn alloc(&self, l: Layout) -> *mut u8 {
        let p = if ROUTE_SQLITE.load(Ordering::Relaxed) {
            // memsys5 blocks are at least 64-byte aligned. Some host async types
            // require 128-byte alignment; retain the original pointer for those.
            if l.align() > 64 {
                let raw = powersync_sqlite_nostd::malloc(l.size() + l.align() + std::mem::size_of::<usize>());
                if raw.is_null() { return raw; }
                let aligned = ((raw as usize + std::mem::size_of::<usize>() + l.align()-1) & !(l.align()-1)) as *mut u8;
                unsafe { aligned.cast::<*mut u8>().sub(1).write(raw); }
                aligned
            } else { unsafe { powersync_sqlite_nostd::SQLite3Allocator {}.alloc(l) } }
        } else { unsafe { System.alloc(l) } };
        if !p.is_null() { let live = LIVE.fetch_add(l.size(), Ordering::Relaxed) + l.size();
            PEAK.fetch_max(live, Ordering::Relaxed); }
        p
    }
    unsafe fn dealloc(&self, p: *mut u8, l: Layout) {
        LIVE.fetch_sub(l.size(), Ordering::Relaxed);
        let base = std::ptr::addr_of_mut!(ARENA).cast::<u8>() as usize;
        if (base..base + 512*1024).contains(&(p as usize)) {
            if l.align() > 64 {
                powersync_sqlite_nostd::free(unsafe { p.cast::<*mut u8>().sub(1).read() }.cast());
            } else { unsafe { powersync_sqlite_nostd::SQLite3Allocator {}.dealloc(p, l) } }
        } else { unsafe { System.dealloc(p, l) } }
    }
}
#[global_allocator]
static ALLOCATOR: CountingAllocator = CountingAllocator;

struct ProofLogger;
impl log::Log for ProofLogger {
    fn enabled(&self, metadata: &log::Metadata<'_>) -> bool { metadata.level() <= log::Level::Warn }
    fn log(&self, record: &log::Record<'_>) {
        if self.enabled(record.metadata()) { eprintln!("{}: {}", record.level(), record.args()); }
    }
    fn flush(&self) {}
}
static LOGGER: ProofLogger = ProofLogger;

struct TimerRegistration { id: u64, deadline: Duration, waker: Waker }
struct ClockState { now: Duration, next_id: u64, timers: Vec<TimerRegistration> }
static CLOCK: Mutex<ClockState> = Mutex::new(ClockState { now: Duration::ZERO, next_id: 0, timers: Vec::new() });
static NEEDS_POLL: AtomicBool = AtomicBool::new(false);
static TASKS: EmbassyTaskPool<4> = EmbassyTaskPool::new();

// The binding, not an Embassy HAL, owns the pender. It only requests a later
// foreground poll, so wakeups cannot re-enter the SDK or Python VM.
#[unsafe(export_name = "__pender")]
fn pender(_context: *mut ()) { NEEDS_POLL.store(true, Ordering::Release); }

fn platform_delay(duration: Duration) -> Boxed<()> {
    let mut state = CLOCK.lock().unwrap();
    let deadline = state.now + duration;
    let id = state.next_id;
    state.next_id += 1;
    Box::pin(Delay { deadline, id })
}
struct Delay { deadline: Duration, id: u64 }
impl Future for Delay {
    type Output = ();
    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<()> {
        let mut s = CLOCK.lock().unwrap();
        if s.now >= self.deadline { return Poll::Ready(()); }
        if let Some(timer) = s.timers.iter_mut().find(|timer| timer.id == self.id) {
            timer.waker.clone_from(cx.waker());
        } else {
            s.timers.push(TimerRegistration { id: self.id, deadline: self.deadline, waker: cx.waker().clone() });
        }
        Poll::Pending
    }
}
impl Drop for Delay {
    fn drop(&mut self) {
        let mut s = CLOCK.lock().unwrap();
        if let Some(index) = s.timers.iter().position(|timer| timer.id == self.id) { s.timers.swap_remove(index); }
    }
}
struct ForegroundRuntime { executor: &'static Executor, polls: AtomicUsize, max_poll_us: AtomicUsize }
impl ForegroundRuntime {
    fn tick(&self) -> bool {
        if NEEDS_POLL.swap(false, Ordering::AcqRel) {
            let started = Instant::now();
            // This is the only poll site. Pender/timer/socket callbacks cannot
            // poll directly, so the raw executor's non-reentrancy rule holds.
            unsafe { self.executor.poll() };
            self.polls.fetch_add(1, Ordering::Relaxed);
            self.max_poll_us.fetch_max(started.elapsed().as_micros() as usize, Ordering::Relaxed);
            true
        } else { false }
    }
    fn advance(&self, duration: Duration) {
        let mut s = CLOCK.lock().unwrap();
        s.now += duration;
        let now = s.now;
        let mut ready = vec![];
        let mut index = 0;
        while index < s.timers.len() {
            if s.timers[index].deadline <= now { ready.push(s.timers.swap_remove(index).waker); }
            else { index += 1; }
        }
        drop(s);
        for w in ready { w.wake(); }
    }
    fn run<F: Future>(&self, f: F) -> F::Output {
        let mut f = std::pin::pin!(f);
        let mut cx = Context::from_waker(Waker::noop());
        for _ in 0..100_000 {
            if let Poll::Ready(r) = f.as_mut().poll(&mut cx) { return r; }
            if !self.tick() { self.advance(Duration::from_millis(1)); }
        }
        panic!("foreground proof stalled");
    }
    fn drain(&self) { for _ in 0..1000 { if !self.tick() { return; } } panic!("executor never idle"); }
}

struct PendingResponse { request: Value, chunks: async_channel::Sender<Result<Bytes, PowerSyncError>> }
#[derive(Clone)]
struct FixtureHttp { requests: async_channel::Sender<PendingResponse>, active: Arc<AtomicUsize> }
struct FixtureBody { receive: Pin<Box<async_channel::Receiver<Result<Bytes, PowerSyncError>>>>, active: Arc<AtomicUsize> }
impl Stream for FixtureBody {
    type Item = Result<Bytes, PowerSyncError>;
    fn poll_next(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        self.receive.as_mut().poll_next(cx)
    }
}
impl Drop for FixtureBody { fn drop(&mut self) { self.active.fetch_sub(1, Ordering::Relaxed); } }
#[async_trait]
impl HttpClient for FixtureHttp {
    async fn send(&self, request: Request) -> Result<Response, PowerSyncError> {
        assert_eq!(request.url.as_str(), "https://badge.fixture.invalid/sync/stream");
        assert_eq!(request.method, "POST");
        assert!(request.headers.iter().any(|(k,v)| *k == "Authorization" && v == "Token fixture-only"));
        let request = serde_json::from_slice(&request.body.unwrap()).unwrap();
        let (send, recv) = async_channel::bounded(1);
        self.requests.send(PendingResponse { request, chunks: send }).await.unwrap();
        self.active.fetch_add(1, Ordering::Relaxed);
        Ok(Response { status: 200, content_type: Some("application/x-ndjson".into()),
            body: ResponseBody { reader: Box::pin(FixtureBody { receive: Box::pin(recv), active: self.active.clone() }), length: None } })
    }
}
#[derive(Clone, Default)]
struct FixtureConnector { credentials: Arc<AtomicUsize> }
#[async_trait]
impl BackendConnector for FixtureConnector {
    async fn fetch_credentials(&self) -> Result<PowerSyncCredentials, PowerSyncError> {
        self.credentials.fetch_add(1, Ordering::Relaxed);
        Ok(PowerSyncCredentials { endpoint: "https://badge.fixture.invalid/".into(), token: "fixture-only".into() })
    }
    async fn upload_data(&self) -> Result<(), PowerSyncError> {
        panic!("game moves must never become SQL CRUD uploads")
    }
}

async fn scalar(db: &PowerSyncDatabase, query: &str) -> i64 {
    let conn = db.reader().await.unwrap();
    // SAFETY: the lease remains held, no await follows, and the statement is
    // finalized before the lease is released; the result is copied as an i64.
    let stmt = unsafe { conn.sqlite_connection().prepare_unchecked(query) }.unwrap();
    assert_eq!(stmt.step().unwrap(), ResultCode::ROW);
    stmt.column_int64(0)
}
async fn send_line(request: &PendingResponse, line: &Value, fragment: usize) {
    let mut bytes = serde_json::to_vec(line).unwrap(); bytes.push(b'\n');
    send_bytes(request, &bytes, fragment).await;
}
async fn send_bytes(request: &PendingResponse, bytes: &[u8], fragment: usize) {
    for chunk in bytes.chunks(fragment) {
        request.chunks.send(Ok(Bytes::copy_from_slice(chunk))).await.unwrap();
    }
}

#[repr(align(64))]
struct Arena([u8; 512*1024]);
static mut ARENA: Arena = Arena([0; 512*1024]);

fn main() {
    log::set_logger(&LOGGER).unwrap();
    log::set_max_level(log::LevelFilter::Warn);
    let fixture: Value = serde_json::from_str(include_str!("../fixture.json")).unwrap();
    let arena_kib: i32 = std::env::var("PROOF_ARENA_KIB").unwrap_or("384".into()).parse().unwrap();
    assert!((256..=512).contains(&arena_kib));
    unsafe {
        assert_eq!(sql::sqlite3_config(8, std::ptr::addr_of_mut!(ARENA.0).cast::<std::ffi::c_void>(), arena_kib*1024, 64_i32), 0);
        assert_eq!(sql::sqlite3_threadsafe(), 0);
    }
    // All core and SDK allocations now use the same compatible SQLite allocator.
    // Test fixture/std startup allocations made earlier retain System provenance.
    ROUTE_SQLITE.store(true, Ordering::Relaxed);
    PowerSyncEnvironment::powersync_auto_extension().unwrap();
    let executor = Box::leak(Box::new(Executor::new(std::ptr::null_mut())));
    let runtime = ForegroundRuntime { executor, polls: AtomicUsize::new(0), max_poll_us: AtomicUsize::new(0) };
    let embassy = EmbassyRuntime::new(executor.spawner().make_send(), &TASKS, platform_delay);
    let (requests_tx, requests) = async_channel::bounded(2);
    let active = Arc::new(AtomicUsize::new(0));
    let http = FixtureHttp { requests: requests_tx, active: active.clone() };
    let raw = RawSqliteConnection::open(c":memory:", sql::SQLITE_OPEN_CREATE | sql::SQLITE_OPEN_READWRITE).unwrap();
    let conn = SqliteConnection::from(raw);
    conn.exec(c"PRAGMA page_size=1024; PRAGMA cache_size=32; PRAGMA max_page_count=128; PRAGMA synchronous=FULL;").unwrap();
    for statement in fixture["raw_sql"].as_array().unwrap() {
        conn.exec(&std::ffi::CString::new(statement.as_str().unwrap()).unwrap()).unwrap();
    }
    let schema = serde_json::value::RawValue::from_string(fixture["schema"].to_string()).unwrap();
    let db = PowerSyncDatabase::new(PowerSyncEnvironment::custom(http,
        ConnectionPool::single_connection(conn), embassy), schema.as_ref());
    let connector = FixtureConnector::default();
    let mut options = SyncOptions::new(connector.clone());
    options.set_include_default_streams(false);
    options.with_retry_delay(Duration::from_millis(5));
    let mut checkpoint = fixture["checkpoint"].clone();
    checkpoint["checkpoint"]["buckets"][0]["priority"] = json!(3);
    let wire = fixture["wire"].as_str().unwrap().as_bytes();
    let split = wire.iter().position(|&b| b == b'\n').unwrap() + 1;
    let expected = fixture["operations"].as_array().unwrap();
    let mut timings = vec![];
    let mut rust_peaks = vec![];
    let mut rust_baselines = vec![];
    runtime.run(async {
        for repetition in 0..21 {
            let subscription = db.sync_stream("badge_game_v2", Some(&json!({"room_id": fixture["room"]})))
                .subscribe().await.unwrap();
            db.connect(options.clone()).await;
            let request = requests.recv().await.unwrap();
            assert_eq!(request.request["streams"]["include_defaults"], false);
            assert_eq!(request.request["streams"]["subscriptions"][0]["stream"], "badge_game_v2");
            assert_eq!(request.request["streams"]["subscriptions"][0]["parameters"]["room_id"], fixture["room"]);
            send_line(&request, &checkpoint, 7).await;
            // Ensure checkpoint metadata was consumed before timing the same 9,114-byte wire.
            while !db.status().is_downloading() { futures_lite::future::yield_now().await; }
            let baseline_heap = LIVE.load(Ordering::Relaxed);
            rust_baselines.push(baseline_heap);
            PEAK.store(baseline_heap, Ordering::Relaxed);
            let start = Instant::now();
            send_bytes(&request, &wire[..split], 2048).await;
            // The data has entered the core, but must not be visible before publication.
            while scalar(&db, "SELECT count(*) FROM ps_oplog").await < 50 {
                futures_lite::future::yield_now().await;
            }
            assert_eq!(scalar(&db, "SELECT count(*) FROM moves").await, 0);
            assert_eq!(scalar(&db, "SELECT count(*) FROM rooms").await, 0);
            send_bytes(&request, &wire[split..], 2048).await;
            subscription.wait_for_first_sync().await;
            while !db.status().has_synced() || db.status().is_downloading() {
                futures_lite::future::yield_now().await;
            }
            timings.push(start.elapsed().as_micros() as u64);
            rust_peaks.push(PEAK.load(Ordering::Relaxed).saturating_sub(baseline_heap));
            assert_eq!(scalar(&db, "SELECT count(*) FROM moves").await, 49);
            assert_eq!(scalar(&db, "SELECT count(*) FROM rooms").await, 1);
            assert_eq!(scalar(&db, "SELECT count(*) FROM ps_crud").await, 0);
            // Exact typed, bound queries validate every published compact move and room field.
            {
                let conn = db.reader().await.unwrap();
                for operation in expected {
                    let table = operation["object_type"].as_str().unwrap();
                    let id = operation["object_id"].as_str().unwrap();
                    let data: Value = serde_json::from_str(operation["data"].as_str().unwrap()).unwrap();
                    for (column, expected_value) in data.as_object().unwrap() {
                        // SAFETY: this loop holds the lease without yielding;
                        // bound IDs outlive the statement, borrowed text is used
                        // immediately, and stmt drops before either the binding
                        // or connection lease can be released.
                        let stmt = unsafe { conn.sqlite_connection().prepare_unchecked(&format!("SELECT {column} FROM {table} WHERE id=?")) }.unwrap();
                        stmt.bind_text(1, id, powersync_sqlite_nostd::Destructor::STATIC).unwrap();
                        assert_eq!(stmt.step().unwrap(), ResultCode::ROW);
                        if let Some(text) = expected_value.as_str() { assert_eq!(stmt.column_text(0).unwrap(), text); }
                        else { assert_eq!(stmt.column_int64(0), expected_value.as_i64().unwrap()); }
                        assert_eq!(stmt.step().unwrap(), ResultCode::DONE);
                    }
                }
            }
            // Inject a transport failure, let the real download loop retry and re-fetch credentials.
            if repetition == 0 {
                request.chunks.send(Err(ResultCode::IOERR.into())).await.unwrap();
                while active.load(Ordering::Relaxed) != 0 { futures_lite::future::yield_now().await; }
                // ForegroundRuntime advances logical time when idle; retry remains upstream code.
                let resumed = requests.recv().await.unwrap();
                assert!(connector.credentials.load(Ordering::Relaxed) >= 2);
                assert_eq!(resumed.request["buckets"][0]["name"], "fixture");
                assert_eq!(resumed.request["buckets"][0]["after"], "50");
                // Advance the recovered cursor with a second, complete publication.
                let mut next_checkpoint = checkpoint.clone();
                next_checkpoint["checkpoint"]["last_op_id"] = json!("51");
                next_checkpoint["checkpoint"]["buckets"][0]["count"] = json!(51);
                send_line(&resumed, &next_checkpoint, 7).await;
                while !db.status().is_downloading() { futures_lite::future::yield_now().await; }
                let mut replay = expected[0].clone(); replay["op_id"] = json!("51");
                send_line(&resumed, &json!({"data": {"bucket": "fixture", "data": [replay]}}), 7).await;
                send_line(&resumed, &json!({"checkpoint_complete": {"last_op_id": "51"}}), 7).await;
                while db.status().is_downloading() { futures_lite::future::yield_now().await; }
                assert_eq!(scalar(&db, "SELECT count(*) FROM moves").await, 49);
                db.disconnect().await;
                assert!(resumed.chunks.is_closed());
            } else {
                db.disconnect().await;
                assert!(request.chunks.is_closed());
            }
            assert_eq!(active.load(Ordering::Relaxed), 0);
            // Hard clear invalidates subscriptions: explicitly subscribe again next round.
            drop(subscription);
            db.disconnect_and_clear(Default::default()).await.unwrap();
        }
    });
    runtime.drain();
    assert_eq!(active.load(Ordering::Relaxed), 0);
    assert!(CLOCK.lock().unwrap().timers.is_empty(), "cancellation must remove timer registrations");
    ROUTE_SQLITE.store(false, Ordering::Relaxed);
    let mut sorted = timings.clone(); sorted.sort_unstable();
    println!("{}", serde_json::to_string_pretty(&json!({
        "scope": "macOS no_std SDK + Embassy raw foreground executor + in-memory SQLite/core, no Wi-Fi/TLS/flash; metadata/open/schema excluded; includes pre-publication assertions",
        "sdk_revision": fixture["sdk_revision"], "wire_bytes": wire.len(), "operations": 50,
        "sdk_source_state": fixture["sdk_source_state"], "sdk_source_digest": fixture["sdk_source_digest"],
        "sdk_default_features": false, "sdk_features": ["embassy"], "embassy_task_pool_slots": 4,
        "transport_fragment_bytes": 2048, "repetitions": timings.len(), "microseconds": timings,
        "median_microseconds": sorted[sorted.len()/2], "rust_incremental_peak_bytes": rust_peaks,
        "rust_live_before_download_bytes": rust_baselines,
        "allocator_scope": "all SDK/core and post-setup fixture Rust allocations share SQLite heap; Rust counters include test code, omit allocator overhead",
        "sqlite_shared_configured_bytes": arena_kib*1024,
        "host_arena_buffer_bytes": 512*1024,
        "sqlite_shared_peak_bytes": unsafe { sql::sqlite3_memory_highwater(0) },
        "foreground_executor_polls": runtime.polls.load(Ordering::Relaxed),
        "longest_executor_poll_microseconds": runtime.max_poll_us.load(Ordering::Relaxed),
        "credentials_calls": connector.credentials.load(Ordering::Relaxed),
        "checks": ["explicit room subscription", "fragmented metadata and data", "invisible before checkpoint", "all 50 published rows/fields", "empty SQL outbox", "transport-error retry", "credential refresh", "resumes cursor 50 and publishes checkpoint 51", "disconnect closes stream", "21 lifecycle rounds", "cancelled timers removed", "Embassy minimum4slot pool reused"]
    })).unwrap());
}
