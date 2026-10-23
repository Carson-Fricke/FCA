//! Queue notification adapters. Admission remains durable before any hint.
use super::Wake;
use crate::http::launcher::BacktestLauncher;
use crate::http::launcher::LaunchError;
use crate::http::launcher::LaunchJob;
use crate::http::screener::ScreenLaunchJob;
use crate::http::screener::ScreenLauncher;
use std::sync::Arc;

pub struct QueuedLauncher(pub Arc<dyn Wake>);
impl BacktestLauncher for QueuedLauncher {
    fn launch(&self, _job: &LaunchJob) -> Result<(), LaunchError> {
        if let Err(error) = self.0.wake() {
            eprintln!("execution remains queued; capacity notification failed: {error}");
        }
        Ok(())
    }
}
impl ScreenLauncher for QueuedLauncher {
    fn launch(&self, _job: &ScreenLaunchJob) -> Result<(), LaunchError> {
        if let Err(error) = self.0.wake() {
            eprintln!("execution remains queued; capacity notification failed: {error}");
        }
        Ok(())
    }
}

pub struct ConfiguredWake;
static NOTIFYING: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
struct Notifying;
impl Drop for Notifying {
    fn drop(&mut self) {
        NOTIFYING.store(false, std::sync::atomic::Ordering::Release);
    }
}
impl Wake for ConfiguredWake {
    fn wake(&self) -> Result<(), String> {
        if NOTIFYING.swap(true, std::sync::atomic::Ordering::AcqRel) {
            return Ok(());
        }
        let result = std::thread::Builder::new()
            .name("execution-notification".into())
            .spawn(|| {
                let _notifying = Notifying;
                if let Err(error) = notify_pool() {
                    eprintln!("execution capacity notification failed: {error}");
                }
            });
        if let Err(error) = result {
            NOTIFYING.store(false, std::sync::atomic::Ordering::Release);
            return Err(error.to_string());
        }
        Ok(())
    }
}
pub use super::launch::notify_pool;
