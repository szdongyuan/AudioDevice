// Command used to generate portaudio.rs:
/* bindgen portaudio.h -o portaudio.rs \
                       --constified-enum PaHostApiTypeId \
                       --constified-enum PaErrorCode \
                       --blacklist-type PaStreamCallbackResult
*/

#[cfg(any(
    target_os = "macos",
    target_os = "linux",
    target_os = "windows"
))]
mod c_library {
    #[link(name = "portaudio")]
    unsafe extern "C" {}
}

mod portaudio;

pub use portaudio::*;

pub const PA_NO_DEVICE: PaDeviceIndex = -1;

// Sample format
pub type SampleFormat = ::std::os::raw::c_ulong;
pub const PA_FLOAT_32: SampleFormat = 0x00000001;
pub const PA_INT_32: SampleFormat = 0x00000002;
pub const PA_INT_24: SampleFormat = 0x00000004;
pub const PA_INT_16: SampleFormat = 0x00000008;
pub const PA_INT_8: SampleFormat = 0x00000010;
pub const PA_UINT_8: SampleFormat = 0x00000020;
pub const PA_CUSTOM_FORMAT: SampleFormat = 0x00010000;
pub const PA_NON_INTERLEAVED: SampleFormat = 0x80000000;

// Stream flags
pub type StreamFlags = ::std::os::raw::c_ulong;
pub const PA_NO_FLAG: StreamFlags = 0;
pub const PA_CLIP_OFF: StreamFlags = 0x00000001;
pub const PA_DITHER_OFF: StreamFlags = 0x00000002;
pub const PA_NEVER_DROP_INPUT: StreamFlags = 0x00000004;
pub const PA_PRIME_OUTPUT_BUFFERS_USING_STREAM_CALLBACK: StreamFlags = 0x00000008;
pub const PA_PLATFORM_SPECIFIC_FLAGS: StreamFlags = 0xFFFF0000;

// Stream callback flags.
pub type StreamCallbackFlags = ::std::os::raw::c_ulong;
pub const INPUT_UNDERFLOW: StreamCallbackFlags = 0x00000001;
pub const INPUT_OVERFLOW: StreamCallbackFlags = 0x00000002;
pub const OUTPUT_UNDERFLOW: StreamCallbackFlags = 0x00000004;
pub const OUTPUT_OVERFLOW: StreamCallbackFlags = 0x00000008;
pub const PRIMING_OUTPUT: StreamCallbackFlags = 0x00000010;

/// Convert C `*const char` to Rust `&str`.
pub fn c_str_to_str<'a>(
    c_str: *const std::os::raw::c_char,
) -> Result<&'a str, ::std::str::Utf8Error> {
    unsafe { ::std::ffi::CStr::from_ptr(c_str).to_str() }
}

/// Convert Rust string to C string pointer.
///
/// Caller must ensure the backing bytes remain valid.
pub fn str_to_c_str(rust_str: &str) -> *const std::os::raw::c_char {
    rust_str.as_ptr() as *const _
}

pub const PA_CONTINUE: PaStreamCallbackResult = 0;
pub const PA_COMPLETE: PaStreamCallbackResult = 1;
pub const PA_ABORT: PaStreamCallbackResult = 2;

pub type PaStreamCallbackResult = ::std::os::raw::c_int;

