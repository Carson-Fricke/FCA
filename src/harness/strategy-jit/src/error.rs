//! Errors raised by the JIT compile and load seam.

/// One JIT seam failure.
#[derive(Debug)]
pub enum JitError {
    /// A filesystem operation failed.
    Io(String),
    /// The authored source failed to compile.
    Compile {
        /// The source digest the build ran for.
        digest: String,
        /// The tail of the captured build output.
        detail: String,
    },
    /// The compiled artifact could not be located.
    ArtifactMissing {
        /// The source digest.
        digest: String,
        /// The path that was expected.
        expected: String,
    },
    /// The dylib could not be loaded or resolved.
    Load {
        /// The artifact path.
        path: String,
        /// The loader failure detail.
        detail: String,
    },
    /// The artifact's ABI version differs from the host's.
    AbiMismatch {
        /// The host ABI version.
        expected: u32,
        /// The artifact ABI version.
        found: u32,
    },
    /// The artifact is missing a required export.
    Export {
        /// The missing symbol.
        symbol: String,
        /// The loader failure detail.
        detail: String,
    },
    /// The program could not be constructed from its parameters.
    Create(Option<String>),
    /// The screening program failed or its result could not be decoded.
    Screen(Option<String>),
    /// An invalid argument was supplied.
    Invalid(String),
}

impl std::fmt::Display for JitError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(detail) => write!(formatter, "jit workspace io failure: {detail}"),
            Self::Compile { digest, detail } => {
                write!(
                    formatter,
                    "authored program {digest} failed to compile: {detail}"
                )
            }
            Self::ArtifactMissing { digest, expected } => {
                write!(
                    formatter,
                    "authored program {digest} produced no artifact at {expected}"
                )
            }
            Self::Load { path, detail } => {
                write!(
                    formatter,
                    "authored artifact {path} failed to load: {detail}"
                )
            }
            Self::AbiMismatch { expected, found } => {
                write!(
                    formatter,
                    "authored artifact ABI version {found} does not match the host ABI {expected}"
                )
            }
            Self::Export { symbol, detail } => {
                write!(
                    formatter,
                    "authored artifact is missing export {symbol:?}: {detail}"
                )
            }
            Self::Create(detail) => {
                write!(
                    formatter,
                    "authored program creation failed: {}",
                    detail.as_deref().unwrap_or("no details reported")
                )
            }
            Self::Screen(detail) => {
                write!(
                    formatter,
                    "screening program failed: {}",
                    detail.as_deref().unwrap_or("no details reported")
                )
            }
            Self::Invalid(detail) => write!(formatter, "invalid jit request: {detail}"),
        }
    }
}

impl std::error::Error for JitError {}

impl From<std::io::Error> for JitError {
    fn from(error: std::io::Error) -> Self {
        Self::Io(error.to_string())
    }
}

pub(crate) fn artifact_missing(digest: &str, expected: &std::path::Path) -> JitError {
    JitError::ArtifactMissing {
        digest: digest.to_string(),
        expected: expected.display().to_string(),
    }
}

pub(crate) fn load_error(path: &std::path::Path, detail: String) -> JitError {
    JitError::Load {
        path: path.display().to_string(),
        detail,
    }
}

pub(crate) fn export_missing(detail: impl std::fmt::Display, symbol: &str) -> JitError {
    JitError::Export {
        symbol: symbol.to_string(),
        detail: detail.to_string(),
    }
}
