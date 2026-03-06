use std::path::{Path, PathBuf};

fn main() {
    println!("cargo:rerun-if-env-changed=PORTAUDIO_LIB_DIR");
    println!("cargo:rerun-if-env-changed=PORTAUDIO_DLL_DIR");
    println!("cargo:rerun-if-changed=build.rs");

    if std::env::var("CARGO_CFG_WINDOWS").is_err() {
        // Keep the original non-Windows behavior out of scope for this project.
        return;
    }

    let lib_dir = resolve_lib_dir();
    println!("cargo:rustc-link-search=native={}", lib_dir.display());
    println!("cargo:rustc-link-lib=dylib=portaudio");

    // Best-effort: copy portaudio.dll next to the current build output.
    if let Some(dll_dir) = resolve_dll_dir() {
        let dll_src = dll_dir.join("portaudio.dll");
        if dll_src.exists() {
            if let Some(target_dir) = resolve_target_profile_dir() {
                let _ = std::fs::create_dir_all(&target_dir);
                let _ = std::fs::copy(&dll_src, target_dir.join("portaudio.dll"));
            }
        }
    }
}

fn resolve_lib_dir() -> PathBuf {
    if let Ok(p) = std::env::var("PORTAUDIO_LIB_DIR") {
        return PathBuf::from(p);
    }

    let manifest_dir = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    // audio_engine/crates/portaudio-sys2 -> audio_engine
    let root = manifest_dir.parent().and_then(|p| p.parent()).unwrap_or(&manifest_dir);
    root.join("third_party").join("portaudio").join("lib")
}

fn resolve_dll_dir() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("PORTAUDIO_DLL_DIR") {
        return Some(PathBuf::from(p));
    }
    let manifest_dir = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let root = manifest_dir.parent().and_then(|p| p.parent()).unwrap_or(&manifest_dir);
    Some(root.join("third_party").join("portaudio").join("bin"))
}

fn resolve_target_profile_dir() -> Option<PathBuf> {
    let out_dir = PathBuf::from(std::env::var("OUT_DIR").ok()?);
    // OUT_DIR = target\debug\build\...\out
    let mut cur: &Path = &out_dir;
    while let Some(parent) = cur.parent() {
        if parent.ends_with("debug") || parent.ends_with("release") {
            return Some(parent.to_path_buf());
        }
        cur = parent;
    }
    None
}

