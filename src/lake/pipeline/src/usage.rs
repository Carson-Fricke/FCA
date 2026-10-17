//! OS process measurements, separate from pipeline allocation accounting.
use serde_json::{json, Value};

#[cfg(windows)]
pub fn process_usage() -> Value {
    use windows_sys::Win32::{
        Foundation::FILETIME,
        System::{
            ProcessStatus::{GetProcessMemoryInfo, PROCESS_MEMORY_COUNTERS},
            Threading::{GetCurrentProcess, GetProcessTimes},
        },
    };
    let mut times: [FILETIME; 4] = unsafe { std::mem::zeroed() };
    let mut memory: PROCESS_MEMORY_COUNTERS = unsafe { std::mem::zeroed() };
    memory.cb = std::mem::size_of::<PROCESS_MEMORY_COUNTERS>() as u32;
    // Windows fills caller-owned POD structures for the current process only.
    unsafe {
        let process = GetCurrentProcess();
        if GetProcessTimes(
            process,
            &mut times[0],
            &mut times[1],
            &mut times[2],
            &mut times[3],
        ) == 0
            || GetProcessMemoryInfo(
                process,
                &mut memory,
                std::mem::size_of::<PROCESS_MEMORY_COUNTERS>() as u32,
            ) == 0
        {
            return Value::Null;
        }
    }
    let ticks =
        |value: FILETIME| (u64::from(value.dwHighDateTime) << 32) | u64::from(value.dwLowDateTime);
    json!({"cpu_seconds":(ticks(times[2]) + ticks(times[3])) as f64 / 10_000_000.0,
        "rss_bytes":memory.WorkingSetSize,"peak_rss_bytes":memory.PeakWorkingSetSize})
}

#[cfg(unix)]
pub fn process_usage() -> Value {
    let mut usage: libc::rusage = unsafe { std::mem::zeroed() };
    // getrusage writes a POD value owned by this call; RUSAGE_SELF sums all threads.
    if unsafe { libc::getrusage(libc::RUSAGE_SELF, &mut usage) } != 0 {
        return Value::Null;
    }
    let seconds = |value: libc::timeval| value.tv_sec as f64 + value.tv_usec as f64 / 1_000_000.0;
    #[cfg(target_os = "linux")]
    let rss = std::fs::read_to_string("/proc/self/status")
        .ok()
        .and_then(|status| {
            status.lines().find_map(|line| {
                line.strip_prefix("VmRSS:")
                    .and_then(|value| value.split_whitespace().next())
                    .and_then(|value| value.parse::<u64>().ok())
                    .map(|kib| kib * 1024)
            })
        });
    #[cfg(not(target_os = "linux"))]
    let rss: Option<u64> = None;
    #[cfg(target_os = "macos")]
    let peak = usage.ru_maxrss as u64;
    #[cfg(not(target_os = "macos"))]
    let peak = usage.ru_maxrss as u64 * 1024;
    json!({"cpu_seconds":seconds(usage.ru_utime) + seconds(usage.ru_stime),"rss_bytes":rss,"peak_rss_bytes":peak})
}

#[cfg(not(any(windows, unix)))]
pub fn process_usage() -> Value {
    Value::Null
}
