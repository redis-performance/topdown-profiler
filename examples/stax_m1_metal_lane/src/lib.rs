use std::ffi::{CStr, c_char, c_void};
use std::ptr;
use std::sync::OnceLock;

use stax_target::{CapturedOrigin, Lane};

fn lane() -> &'static Lane {
    static LANE: OnceLock<Lane> = OnceLock::new();
    LANE.get_or_init(|| Lane::metal("M1 Metal command buffers"))
}

#[unsafe(no_mangle)]
pub extern "C" fn stax_metal_begin() -> *mut c_void {
    let capture = lane().capture_origin();
    if capture.is_active() {
        Box::into_raw(Box::new(capture)).cast()
    } else {
        ptr::null_mut()
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn stax_metal_finish(
    token: *mut c_void,
    name: *const c_char,
    start_seconds: f64,
    end_seconds: f64,
) -> bool {
    if token.is_null() {
        return false;
    }
    let captured = unsafe { *Box::from_raw(token.cast::<CapturedOrigin>()) };
    if name.is_null()
        || !start_seconds.is_finite()
        || !end_seconds.is_finite()
        || start_seconds <= 0.0
        || end_seconds <= start_seconds
        || end_seconds > u64::MAX as f64 / 1e9
    {
        return false;
    }
    let Ok(name) = unsafe { CStr::from_ptr(name) }.to_str() else {
        return false;
    };
    let start_ns = (start_seconds * 1e9).round() as u64;
    let end_ns = (end_seconds * 1e9).round() as u64;
    let Some(span) = lane().span_with_captured_origin(name, start_ns, end_ns, captured) else {
        return false;
    };
    lane().try_report(vec![span]).is_ok()
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn stax_metal_cancel(token: *mut c_void) {
    if !token.is_null() {
        drop(unsafe { Box::from_raw(token.cast::<CapturedOrigin>()) });
    }
}
