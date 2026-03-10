use std::path::PathBuf;

fn main() {
    // Windows-only crate; build.rs is still executed on non-Windows in some setups,
    // so keep it minimal and avoid failing hard here.
    if std::env::var("CARGO_CFG_WINDOWS").is_err() {
        return;
    }

    // Optional: if user provides a prebuilt PortAudio import library, link to it.
    // This enables the "ship audiodevice.exe + portaudio.dll" distribution flow.
    //
    // Layout:
    //   third_party/portaudio/
    //     include/
    //     lib/portaudio.lib
    //     bin/portaudio.dll
    let manifest_dir = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let pa_dir = manifest_dir.join("third_party").join("portaudio");
    let pa_lib = pa_dir.join("lib").join("portaudio.lib");
    if pa_lib.exists() {
        println!("cargo:rustc-link-search=native={}", pa_dir.join("lib").display());
        println!("cargo:rustc-link-lib=dylib=portaudio");

        // Copy DLL next to the built exe for local runs.
        let dll_src = pa_dir.join("bin").join("portaudio.dll");
        if dll_src.exists() {
            if let (Ok(profile), Ok(target_dir)) = (
                std::env::var("PROFILE"),
                std::env::var("CARGO_TARGET_DIR"),
            ) {
                let out_dir = PathBuf::from(target_dir).join(profile);
                let _ = std::fs::create_dir_all(&out_dir);
                let _ = std::fs::copy(&dll_src, out_dir.join("portaudio.dll"));
            }
        }
    }
}

