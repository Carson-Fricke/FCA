//! Loading compiled authored artifacts through `libloading`.

use std::ffi::{c_char, CStr as FfiCStr};
use std::path::Path;

use harness_strategy_sdk::{
    CStr, ScreenContext, ScreenRow, ScreeningProgramVTable, StrategyProgramVTable,
    StudyProgramVTable, ABI_EXPORT, LAST_ERROR_EXPORT, SCREENING_VTABLE_EXPORT, SDK_ABI_VERSION,
    SDK_OK, STRATEGY_VTABLE_EXPORT, STUDY_VTABLE_EXPORT,
};
use libloading::{Library, Symbol};

use crate::error::{export_missing, load_error, JitError};

/// A loaded study program: the dylib, its vtable, and the constructed state.
///
/// The engine drives the state single-threaded; `Drop` destroys the state and
/// then unloads the library.
pub struct LoadedStudy {
    library: Library,
    vtable: &'static StudyProgramVTable,
    state: *mut std::os::raw::c_void,
}

// The engine constructs, drives, and drops a runtime on one thread; the
// exports are thread-local-error free pure dispatchers over that state.
unsafe impl Send for LoadedStudy {}

impl LoadedStudy {
    /// Load one study artifact and construct its program state.
    ///
    /// # Safety
    ///
    /// The artifact must be a `harness-strategy-sdk` cdylib; the loader
    /// validates the ABI version and the vtable before constructing state.
    ///
    /// # Errors
    ///
    /// Returns [`JitError`] when the library cannot be loaded, the exports
    /// are missing, the ABI version differs, or the program rejects its
    /// parameters.
    pub unsafe fn load(artifact: &Path, parameters: &serde_json::Value) -> Result<Self, JitError> {
        let library = unsafe { Library::new(artifact) }
            .map_err(|error| load_error(artifact, error.to_string()))?;
        let abi_version = unsafe { read_abi_version(&library)? };
        if abi_version != SDK_ABI_VERSION {
            return Err(JitError::AbiMismatch {
                expected: SDK_ABI_VERSION,
                found: abi_version,
            });
        }
        let vtable: &'static StudyProgramVTable = unsafe {
            let symbol: Symbol<unsafe extern "C" fn() -> *const StudyProgramVTable> = library
                .get(STUDY_VTABLE_EXPORT.as_bytes())
                .map_err(|error| export_missing(&error, STUDY_VTABLE_EXPORT))?;
            let pointer = symbol();
            pointer.as_ref().ok_or_else(|| JitError::Export {
                symbol: STUDY_VTABLE_EXPORT.to_string(),
                detail: "vtable pointer is null".to_string(),
            })?
        };
        if vtable.abi_version != SDK_ABI_VERSION {
            return Err(JitError::AbiMismatch {
                expected: SDK_ABI_VERSION,
                found: vtable.abi_version,
            });
        }
        let parameters_json = serde_json::to_string(parameters)
            .map_err(|error| JitError::Invalid(error.to_string()))?;
        let state = unsafe { (vtable.create)(CStr::from_slice(&parameters_json)) };
        if state.is_null() {
            return Err(JitError::Create(unsafe { read_last_error(&library) }));
        }
        Ok(Self {
            library,
            vtable,
            state,
        })
    }

    /// The program vtable.
    #[must_use]
    pub const fn vtable(&self) -> &'static StudyProgramVTable {
        self.vtable
    }

    /// The constructed program state.
    #[must_use]
    pub const fn state(&self) -> *mut std::os::raw::c_void {
        self.state
    }

    /// The recorded last error from the artifact, if any.
    ///
    /// # Safety
    ///
    /// Must be called while the library is loaded.
    pub unsafe fn last_error(&self) -> Option<String> {
        unsafe { read_last_error(&self.library) }
    }
}

impl Drop for LoadedStudy {
    fn drop(&mut self) {
        // Safety: state came from create and is destroyed exactly once here.
        unsafe { (self.vtable.destroy)(self.state) };
    }
}

/// A loaded strategy artifact; see [`LoadedStudy`].
pub struct LoadedStrategy {
    library: Library,
    vtable: &'static StrategyProgramVTable,
    state: *mut std::os::raw::c_void,
}

// See LoadedStudy.
unsafe impl Send for LoadedStrategy {}

impl LoadedStrategy {
    /// Load one strategy artifact and construct its program state.
    ///
    /// # Safety
    ///
    /// The artifact must be a `harness-strategy-sdk` cdylib; the loader
    /// validates the ABI version and the vtable before constructing state.
    ///
    /// # Errors
    ///
    /// Returns [`JitError`] under the same conditions as [`LoadedStudy::load`].
    pub unsafe fn load(artifact: &Path, parameters: &serde_json::Value) -> Result<Self, JitError> {
        let library = unsafe { Library::new(artifact) }
            .map_err(|error| load_error(artifact, error.to_string()))?;
        let abi_version = unsafe { read_abi_version(&library)? };
        if abi_version != SDK_ABI_VERSION {
            return Err(JitError::AbiMismatch {
                expected: SDK_ABI_VERSION,
                found: abi_version,
            });
        }
        let vtable: &'static StrategyProgramVTable = unsafe {
            let symbol: Symbol<unsafe extern "C" fn() -> *const StrategyProgramVTable> = library
                .get(STRATEGY_VTABLE_EXPORT.as_bytes())
                .map_err(|error| export_missing(&error, STRATEGY_VTABLE_EXPORT))?;
            let pointer = symbol();
            pointer.as_ref().ok_or_else(|| JitError::Export {
                symbol: STRATEGY_VTABLE_EXPORT.to_string(),
                detail: "vtable pointer is null".to_string(),
            })?
        };
        if vtable.abi_version != SDK_ABI_VERSION {
            return Err(JitError::AbiMismatch {
                expected: SDK_ABI_VERSION,
                found: vtable.abi_version,
            });
        }
        let parameters_json = serde_json::to_string(parameters)
            .map_err(|error| JitError::Invalid(error.to_string()))?;
        let state = unsafe { (vtable.create)(CStr::from_slice(&parameters_json)) };
        if state.is_null() {
            return Err(JitError::Create(unsafe { read_last_error(&library) }));
        }
        Ok(Self {
            library,
            vtable,
            state,
        })
    }

    /// The program vtable.
    #[must_use]
    pub const fn vtable(&self) -> &'static StrategyProgramVTable {
        self.vtable
    }

    /// The constructed program state.
    #[must_use]
    pub const fn state(&self) -> *mut std::os::raw::c_void {
        self.state
    }

    /// The recorded last error from the artifact, if any.
    ///
    /// # Safety
    ///
    /// Must be called while the library is loaded.
    pub unsafe fn last_error(&self) -> Option<String> {
        unsafe { read_last_error(&self.library) }
    }
}

impl Drop for LoadedStrategy {
    fn drop(&mut self) {
        // Safety: state came from create and is destroyed exactly once here.
        unsafe { (self.vtable.destroy)(self.state) };
    }
}

/// A loaded screening artifact; see [`LoadedStudy`].
///
/// Screening is one-shot: [`screen`](LoadedScreening::screen) serializes one
/// [`ScreenContext`], runs the authored program, and copies the result rows
/// out before the next call or drop. The vtable carries no order surface by
/// construction.
pub struct LoadedScreening {
    library: Library,
    vtable: &'static ScreeningProgramVTable,
    state: *mut std::os::raw::c_void,
}

// See LoadedStudy.
unsafe impl Send for LoadedScreening {}

impl LoadedScreening {
    /// Load one screening artifact and construct its program state.
    ///
    /// # Safety
    ///
    /// The artifact must be a `harness-strategy-sdk` cdylib; the loader
    /// validates the ABI version and the vtable before constructing state.
    ///
    /// # Errors
    ///
    /// Returns [`JitError`] under the same conditions as [`LoadedStudy::load`].
    pub unsafe fn load(artifact: &Path, parameters: &serde_json::Value) -> Result<Self, JitError> {
        let library = unsafe { Library::new(artifact) }
            .map_err(|error| load_error(artifact, error.to_string()))?;
        let abi_version = unsafe { read_abi_version(&library)? };
        if abi_version != SDK_ABI_VERSION {
            return Err(JitError::AbiMismatch {
                expected: SDK_ABI_VERSION,
                found: abi_version,
            });
        }
        let vtable: &'static ScreeningProgramVTable = unsafe {
            let symbol: Symbol<unsafe extern "C" fn() -> *const ScreeningProgramVTable> = library
                .get(SCREENING_VTABLE_EXPORT.as_bytes())
                .map_err(|error| export_missing(&error, SCREENING_VTABLE_EXPORT))?;
            let pointer = symbol();
            pointer.as_ref().ok_or_else(|| JitError::Export {
                symbol: SCREENING_VTABLE_EXPORT.to_string(),
                detail: "vtable pointer is null".to_string(),
            })?
        };
        if vtable.abi_version != SDK_ABI_VERSION {
            return Err(JitError::AbiMismatch {
                expected: SDK_ABI_VERSION,
                found: vtable.abi_version,
            });
        }
        let parameters_json = serde_json::to_string(parameters)
            .map_err(|error| JitError::Invalid(error.to_string()))?;
        let state = unsafe { (vtable.create)(CStr::from_slice(&parameters_json)) };
        if state.is_null() {
            return Err(JitError::Create(unsafe { read_last_error(&library) }));
        }
        Ok(Self {
            library,
            vtable,
            state,
        })
    }

    /// The program vtable.
    #[must_use]
    pub const fn vtable(&self) -> &'static ScreeningProgramVTable {
        self.vtable
    }

    /// The constructed program state.
    #[must_use]
    pub const fn state(&self) -> *mut std::os::raw::c_void {
        self.state
    }

    /// The recorded last error from the artifact, if any.
    ///
    /// # Safety
    ///
    /// Must be called while the library is loaded.
    pub unsafe fn last_error(&self) -> Option<String> {
        unsafe { read_last_error(&self.library) }
    }

    /// Run one screening pass over `context` and return the program's rows.
    ///
    /// The context is serialized and the result payload is copied into owned
    /// rows before this call returns; the artifact's result buffer is never
    /// borrowed past the call.
    ///
    /// # Errors
    ///
    /// Returns [`JitError::Screen`] when the authored program fails, panics,
    /// or returns a payload that does not decode as rows.
    pub fn screen(&mut self, context: &ScreenContext) -> Result<Vec<ScreenRow>, JitError> {
        let document = serde_json::to_string(context).map_err(|error| {
            JitError::Invalid(format!(
                "screening context could not be serialized: {error}"
            ))
        })?;
        let code = unsafe { (self.vtable.screen)(self.state, CStr::from_slice(&document)) };
        if code != SDK_OK {
            return Err(JitError::Screen(unsafe { read_last_error(&self.library) }));
        }
        let view = unsafe { (self.vtable.result)(self.state) };
        let Some(text) = (unsafe { view.as_str() }) else {
            return Err(JitError::Screen(Some(
                "screening result is not valid UTF-8".to_string(),
            )));
        };
        serde_json::from_str::<Vec<ScreenRow>>(text).map_err(|error| {
            JitError::Screen(Some(format!("screening result is not valid JSON: {error}")))
        })
    }
}

impl Drop for LoadedScreening {
    fn drop(&mut self) {
        // Safety: state came from create and is destroyed exactly once here.
        unsafe { (self.vtable.destroy)(self.state) };
    }
}

unsafe fn read_abi_version(library: &Library) -> Result<u32, JitError> {
    unsafe {
        let symbol: Symbol<unsafe extern "C" fn() -> u32> = library
            .get(ABI_EXPORT.as_bytes())
            .map_err(|error| export_missing(&error, ABI_EXPORT))?;
        Ok(symbol())
    }
}

unsafe fn read_last_error(library: &Library) -> Option<String> {
    unsafe {
        let symbol: Symbol<unsafe extern "C" fn() -> *const c_char> =
            library.get(LAST_ERROR_EXPORT.as_bytes()).ok()?;
        let pointer = symbol();
        if pointer.is_null() {
            return None;
        }
        Some(FfiCStr::from_ptr(pointer).to_string_lossy().into_owned())
    }
}
