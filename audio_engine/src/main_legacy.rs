// Legacy engine implementation kept for reference.
// This file is not compiled by default.

use anyhow::{Result, anyhow};
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use rtrb::RingBuffer;
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicU64, Ordering},
    mpsc,
};
use std::thread;
use std::time::Instant;

const DEFAULT_ADDR: &str = "127.0.0.1:18789";

#[derive(Debug, Clone)]
struct EngineConfig {
    device_name_contains: String,
    sr: u32,
    in_ch: u16,
    out_ch: u16,
    wav_path: String,
    rb_frames: usize,
    wav_queue_capacity: usize,
}

impl Default for EngineConfig {
    fn default() -> Self {
        Self {
            device_name_contains: "UMC ASIO Driver".to_string(),
            sr: 48_000,
            in_ch: 2,
            out_ch: 2,
            wav_path: "record.wav".to_string(),
            rb_frames: 48_000usize * 2,
            wav_queue_capacity: 128,
        }
    }
}

#[derive(Debug)]
struct Metrics {
    start_t: Instant,
    frames_written: AtomicU64,
    bytes_written: AtomicU64,
    wav_drops: AtomicU64,
    underruns: AtomicU64,
    overruns: AtomicU64,
}

impl Metrics {
    fn new() -> Self {
        Self {
            start_t: Instant::now(),
            frames_written: AtomicU64::new(0),
            bytes_written: AtomicU64::new(0),
            wav_drops: AtomicU64::new(0),
            underruns: AtomicU64::new(0),
            overruns: AtomicU64::new(0),
        }
    }
}

struct EngineHandle {
    _in_stream: cpal::Stream,
    _out_stream: cpal::Stream,
    _tx_wav: mpsc::SyncSender<Vec<i16>>,
}

struct SharedState {
    cfg: EngineConfig,
    running: bool,
    metrics: Arc<Metrics>,
    handle: Option<EngineHandle>,
}

impl SharedState {
    fn new() -> Self {
        Self {
            cfg: EngineConfig::default(),
            running: false,
            metrics: Arc::new(Metrics::new()),
            handle: None,
        }
    }
}

#[derive(Deserialize)]
struct Cmd {
    cmd: String,
    device: Option<String>,
    sr: Option<u32>,
    in_ch: Option<u16>,
    out_ch: Option<u16>,
    path: Option<String>,
    rb_frames: Option<usize>,
    wav_queue_capacity: Option<usize>,
    device_name_contains: Option<String>,
}

#[derive(Serialize)]
struct Reply<T: Serialize> {
    ok: bool,
    data: T,
    err: Option<String>,
}

fn reply_ok<T: Serialize>(data: T) -> String {
    serde_json::to_string(&Reply {
        ok: true,
        data,
        err: None,
    })
    .unwrap()
        + "\n"
}

fn reply_err(msg: impl Into<String>) -> String {
    serde_json::to_string(&Reply::<serde_json::Value> {
        ok: false,
        data: json!({}),
        err: Some(msg.into()),
    })
    .unwrap()
        + "\n"
}

fn main() -> Result<()> {
    println!("Legacy engine listening on {}", DEFAULT_ADDR);

    let state = Arc::new(Mutex::new(SharedState::new()));
    let listener = TcpListener::bind(DEFAULT_ADDR)?;
    for conn in listener.incoming() {
        match conn {
            Ok(stream) => {
                let st = state.clone();
                thread::spawn(move || {
                    if let Err(e) = handle_client(stream, st) {
                        eprintln!("client error: {e}");
                    }
                });
            }
            Err(e) => eprintln!("accept error: {e}"),
        }
    }
    Ok(())
}

fn handle_client(stream: TcpStream, state: Arc<Mutex<SharedState>>) -> Result<()> {
    let reader = BufReader::new(stream.try_clone()?);
    let mut writer = BufWriter::new(stream);

    for line in reader.lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }

        let cmd: Cmd = match serde_json::from_str(&line) {
            Ok(v) => v,
            Err(e) => {
                writer.write_all(reply_err(format!("bad json: {e}")).as_bytes())?;
                writer.flush()?;
                continue;
            }
        };

        let resp = dispatch(cmd, &state);
        writer.write_all(resp.as_bytes())?;
        writer.flush()?;
    }

    Ok(())
}

fn dispatch(cmd: Cmd, state: &Arc<Mutex<SharedState>>) -> String {
    match cmd.cmd.as_str() {
        "list_hosts" => {
            let hosts: Vec<String> = cpal::available_hosts()
                .into_iter()
                .map(|h| h.name().to_string())
                .collect();
            reply_ok(json!({ "hosts": hosts }))
        }
        "list_devices" => {
            let mut out = serde_json::Map::new();
            for host_id in cpal::available_hosts() {
                if let Ok(host) = cpal::host_from_id(host_id) {
                    let mut devs = vec![];
                    if let Ok(iter) = host.devices() {
                        for d in iter {
                            if let Ok(name) = d.name() {
                                devs.push(name);
                            }
                        }
                    }
                    out.insert(host.id().name().to_string(), json!(devs));
                }
            }
            reply_ok(json!({ "devices": out }))
        }
        "status" => {
            let st = state.lock().unwrap();
            let m = &st.metrics;
            let secs = m.start_t.elapsed().as_secs_f64();
            reply_ok(json!({
                "running": st.running,
                "metrics": {
                    "uptime_s": secs,
                    "frames_written": m.frames_written.load(Ordering::Relaxed),
                    "bytes_written": m.bytes_written.load(Ordering::Relaxed),
                    "wav_drops": m.wav_drops.load(Ordering::Relaxed),
                    "underruns": m.underruns.load(Ordering::Relaxed),
                    "overruns": m.overruns.load(Ordering::Relaxed),
                }
            }))
        }
        _ => reply_err("unknown cmd"),
    }
}

