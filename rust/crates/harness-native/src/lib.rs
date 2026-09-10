//! Native core for the quant-harness Rust engine.
//!
//! This crate hosts the C ABI surface that will grow into the ported Zig core.
//! It currently exposes a single placeholder symbol so the `cdylib` artifact
//! is loadable; the real event-kernel, OMS, replay, and quant surfaces follow
//! in later tasks.
//!
//! The Python engine and the pinned Zig native core remain authoritative until
//! the migration flips.

use core::ffi::c_char;

/// Semantic version of this crate as a NUL-terminated C string.
static VERSION: &[u8] = b"0.1.0\0";

/// Returns a pointer to the crate's NUL-terminated version string.
///
/// Placeholder C ABI entry point. The returned pointer references static
/// storage owned by this library; callers must not free or mutate it.
#[no_mangle]
pub extern "C" fn harness_native_version() -> *const c_char {
    VERSION.as_ptr().cast::<c_char>()
}
