#![cfg(windows)]

mod rpc;
mod engine;
mod backends;
mod tasks;
mod audio;

use anyhow::Result;
use std::sync::Arc;

const DEFAULT_ADDR: &str = "127.0.0.1:18789";

fn main() -> Result<()> {
    println!("audiodevice engine starting (Windows-only).");
    println!("Listening on {}", DEFAULT_ADDR);

    let engine = Arc::new(engine::state::Engine::new());

    {
        let engine = engine.clone();
        ctrlc::set_handler(move || {
            eprintln!("Ctrl+C received. Stopping session...");
            let _ = engine.session_stop();
        })?;
    }

    rpc::serve(DEFAULT_ADDR, engine)?;
    Ok(())
}
