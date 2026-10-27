//! Compile-only subset: this is not the PowerSync native SDK or a badge app.
#![no_std]
extern crate alloc;

use alloc::sync::Arc;
use async_task::{Runnable, Task};

// Uses the same scheduling handle family as PowerSyncEnvironment::custom.
pub fn spawn_probe(schedule: impl Fn(Runnable) + Send + Sync + 'static) -> Task<()> {
    let (runnable, task) = async_task::spawn(async {}, schedule);
    runnable.schedule();
    task
}

pub async fn channel_lock_probe() -> usize {
    let (sender, receiver) = async_channel::bounded(1);
    let value = Arc::new(async_lock::Mutex::new(1usize));
    sender.send(value).await.unwrap();
    let value = receiver.recv().await.unwrap();
    *value.lock().await
}

pub fn event_probe() -> event_listener::Event {
    event_listener::Event::new()
}

pub fn boxed_probe() -> futures_lite::future::Boxed<()> {
    use futures_lite::FutureExt;
    async {}.boxed()
}
