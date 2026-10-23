//! Synchronous validator port over one bounded compiler child. A faulty authored
//! constructor, timeout or allocation failure cannot terminate the API process.
use mcp::tools::ports::StudyValidator;
use mcp::tools::ports::ToolExecutionError;
use serde_json::json;
use serde_json::Map;
use serde_json::Value;
use std::io::Read;
use std::io::Write;
use std::process::Command;
use std::process::Stdio;
use std::sync::Mutex;

pub struct ProcessValidator {
    pub binary: std::path::PathBuf,
}
impl ProcessValidator {
    pub fn configured() -> Result<Self, String> {
        let binary = std::env::var_os("HARNESS_WORKER_EXECUTABLE")
            .map(std::path::PathBuf::from)
            .map(Ok)
            .unwrap_or_else(|| {
                std::env::current_exe()
                    .map(|path| {
                        path.with_file_name(if cfg!(windows) {
                            "harness-worker.exe"
                        } else {
                            "harness-worker"
                        })
                    })
                    .map_err(|e| e.to_string())
            })?;
        Ok(Self { binary })
    }
}

impl StudyValidator for ProcessValidator {
    fn validation_identity(&self) -> Option<String> {
        crate::local::NativeStudyValidator.validation_identity()
    }
    fn validate_strategy_result(&self, model: &Value) -> Result<Value, ToolExecutionError> {
        validate(model, "backtest", &self.binary)
    }
    fn validate_result(&self, model: &Value) -> Result<Value, ToolExecutionError> {
        validate(model, "study", &self.binary)
    }
    fn validate(&self, model: &Value) -> Result<Value, String> {
        self.validate_result(model).map_err(|e| e.to_string())
    }
}
fn failure(error: impl std::fmt::Display) -> ToolExecutionError {
    ToolExecutionError::new(error.to_string())
}
fn validate(
    model: &Value,
    shell: &str,
    binary: &std::path::Path,
) -> Result<Value, ToolExecutionError> {
    crate::local::cached_authored_validation(model, shell, || isolated(model, shell, binary))
}
fn isolated(
    model: &Value,
    shell: &str,
    binary: &std::path::Path,
) -> Result<Value, harness_strategy_jit::ValidationFailure> {
    isolated_response(model, shell, binary)
        .map_err(|error| harness_strategy_jit::ValidationFailure::new(error.to_string()))?
}
fn isolated_response(
    model: &Value,
    shell: &str,
    binary: &std::path::Path,
) -> Result<Result<Value, harness_strategy_jit::ValidationFailure>, ToolExecutionError> {
    static COMPILER: Mutex<()> = Mutex::new(());
    let _compiler = COMPILER
        .lock()
        .map_err(|_| failure("compiler admission is unavailable"))?;
    let mut command = Command::new(binary);
    command
        .arg("--validate")
        .env("CARGO_BUILD_JOBS", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
        // SAFETY: only async-signal-safe libc calls run between fork and exec.
        unsafe {
            command.pre_exec(|| {
                let limit = libc::rlimit {
                    rlim_cur: 1024 * 1024 * 1024,
                    rlim_max: 1024 * 1024 * 1024,
                };
                if libc::setrlimit(libc::RLIMIT_DATA, &limit) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000);
    }
    let mut child = ChildGuard(command.spawn().map_err(failure)?);
    #[cfg(windows)]
    let _limit = windows_limit(&child).map_err(failure)?;
    let request = serde_json::to_vec(&json!({"shell":shell,"model":model})).map_err(failure)?;
    child
        .stdin
        .take()
        .ok_or_else(|| failure("validator input unavailable"))?
        .write_all(&request)
        .map_err(failure)?;
    let output = child
        .stdout
        .take()
        .ok_or_else(|| failure("validator output unavailable"))?;
    let (sender, receiver) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let result = output
            .take(8 * 1024 * 1024 + 1)
            .read_to_end(&mut bytes)
            .map(|_| bytes);
        let _ = sender.send(result);
    });
    let received = receiver.recv_timeout(std::time::Duration::from_secs(180));
    let result = match received {
        Ok(result) => result.map_err(failure),
        Err(_) => {
            #[cfg(unix)]
            {
                // SAFETY: the child owns this newly created process group.
                unsafe {
                    libc::kill(-(child.id() as i32), libc::SIGKILL);
                }
            }
            let _ = child.kill();
            Err(failure(
                "validation exceeded its isolated compiler deadline",
            ))
        }
    };
    let status = child.wait().map_err(failure)?;
    let bytes = result?;
    if !status.success() || bytes.len() > 8 * 1024 * 1024 {
        return Err(failure(
            "isolated validator terminated or exceeded output capacity",
        ));
    }
    serde_json::from_slice(&bytes)
        .map_err(|_| failure("isolated validator returned an invalid response"))
}

/// Worker-only JSON transport. It does not compose an API or admit execution work.
pub fn serve() -> Result<(), String> {
    let mut bytes = Vec::new();
    std::io::stdin()
        .take(8 * 1024 * 1024 + 1)
        .read_to_end(&mut bytes)
        .map_err(|e| e.to_string())?;
    if bytes.len() > 8 * 1024 * 1024 {
        return Err("validation input exceeds capacity".into());
    }
    let request: Value = serde_json::from_slice(&bytes).map_err(|e| e.to_string())?;
    let result = match request["shell"].as_str() {
        Some(shell @ ("backtest" | "study")) => {
            crate::local::inspect_authored_program(&request["model"], shell)
        }
        _ => return Err("unknown validation shell".into()),
    };
    std::io::stdout()
        .write_all(&serde_json::to_vec(&result).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())
}

#[cfg(windows)]
struct MemoryLimit(windows_sys::Win32::Foundation::HANDLE);
#[cfg(windows)]
impl Drop for MemoryLimit {
    fn drop(&mut self) {
        // SAFETY: this guard owns the job handle.
        unsafe {
            windows_sys::Win32::Foundation::CloseHandle(self.0);
        }
    }
}
#[cfg(windows)]
fn windows_limit(child: &std::process::Child) -> std::io::Result<MemoryLimit> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::System::JobObjects::*;
    // SAFETY: all structures are initialized, and the child waits for its input
    // until after it and its compiler descendants are assigned to this job.
    unsafe {
        let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if handle.is_null() {
            return Err(std::io::Error::last_os_error());
        }
        let guard = MemoryLimit(handle);
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
        limits.BasicLimitInformation.LimitFlags =
            JOB_OBJECT_LIMIT_JOB_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        limits.JobMemoryLimit = 1024 * 1024 * 1024;
        if SetInformationJobObject(
            handle,
            JobObjectExtendedLimitInformation,
            &limits as *const _ as *const _,
            std::mem::size_of_val(&limits) as u32,
        ) == 0
            || AssignProcessToJobObject(handle, child.as_raw_handle()) == 0
        {
            return Err(std::io::Error::last_os_error());
        }
        Ok(guard)
    }
}

struct ChildGuard(std::process::Child);
impl std::ops::Deref for ChildGuard {
    type Target = std::process::Child;
    fn deref(&self) -> &Self::Target {
        &self.0
    }
}
impl std::ops::DerefMut for ChildGuard {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.0
    }
}
impl Drop for ChildGuard {
    fn drop(&mut self) {
        if !matches!(self.0.try_wait(), Ok(Some(_))) {
            #[cfg(unix)]
            {
                // SAFETY: this is the owned, still-running child process group.
                unsafe {
                    libc::kill(-(self.0.id() as i32), libc::SIGKILL);
                }
            }
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
}

impl mcp::tools::ports::SdkReference for ProcessValidator {
    fn sdk_reference(&self, args: &Map<String, Value>) -> Result<Value, ToolExecutionError> {
        crate::local::NativeStudyValidator.sdk_reference(args)
    }
}
