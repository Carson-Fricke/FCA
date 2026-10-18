//! Local filesystem and finite-process adapters for the complete harness deployment.
mod catalog;
mod jobs;
mod ownership;
mod runner;
mod state;
pub use catalog::FileCatalog;
pub use jobs::LocalJobStore;
pub use ownership::FileLeaseProvider;
pub use runner::LocalRunner;
pub use state::{LocalLayout, LocalStateError, MARKER_CONTENT, MARKER_NAME};

/// Snapshot an ingestion budget from currently available host RAM, retaining OS/application headroom.
pub fn host_memory_mib() -> Result<usize, lake_contracts::DataError> {
    #[cfg(windows)]
    let available = {
        use windows_sys::Win32::System::SystemInformation::{GlobalMemoryStatusEx, MEMORYSTATUSEX};
        let mut state: MEMORYSTATUSEX = unsafe { std::mem::zeroed() };
        state.dwLength = std::mem::size_of::<MEMORYSTATUSEX>() as u32;
        if unsafe { GlobalMemoryStatusEx(&mut state) } == 0 {
            return Err(lake_contracts::DataError::io(
                std::io::Error::last_os_error(),
            ));
        }
        state.ullAvailPhys as usize
    };
    #[cfg(target_os = "linux")]
    let available = std::fs::read_to_string("/proc/meminfo")?
        .lines()
        .find_map(|line| {
            line.strip_prefix("MemAvailable:")
                .and_then(|s| s.split_whitespace().next())
                .and_then(|s| s.parse::<usize>().ok())
                .map(|n| n * 1024)
        })
        .ok_or_else(|| lake_contracts::DataError::value("host available memory unavailable"))?;
    #[cfg(not(any(windows, target_os = "linux")))]
    let available = {
        let pages = unsafe { libc::sysconf(libc::_SC_AVPHYS_PAGES) };
        let size = unsafe { libc::sysconf(libc::_SC_PAGESIZE) };
        if pages <= 0 || size <= 0 {
            return Err(lake_contracts::DataError::value(
                "host available memory unavailable",
            ));
        }
        (pages as usize).saturating_mul(size as usize)
    };
    Ok(available / 1024 / 1024 * 3 / 4)
}
