use crate::audio::convert;
use crate::audio::ring::AudioRing;
use crate::engine::metrics::Metrics;
use crate::engine::routes::SessionParams;
use anyhow::{anyhow, Result};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

pub fn spawn_wav_player(
    mut bus_out: AudioRing,
    stop_flag: Arc<AtomicBool>,
    params: SessionParams,
    metrics: Arc<Metrics>,
) -> Result<thread::JoinHandle<()>> {
    if params.play_path.is_empty() {
        return Err(anyhow!("player requires a non-empty play_path"));
    }
    let handle = thread::spawn(move || {
        let _ = wav_player_loop(&mut bus_out, stop_flag, params, metrics);
    });
    Ok(handle)
}

fn wav_player_loop(
    bus_out: &mut AudioRing,
    stop_flag: Arc<AtomicBool>,
    params: SessionParams,
    metrics: Arc<Metrics>,
) -> Result<()> {
    let mut reader = hound::WavReader::open(&params.play_path)?;
    let spec = reader.spec();
    if spec.channels as u16 != params.out_ch {
        return Err(anyhow!(
            "play_path channels mismatch: file={}, out_ch={}",
            spec.channels,
            params.out_ch
        ));
    }
    if spec.sample_rate != params.sr {
        return Err(anyhow!(
            "play_path sample_rate mismatch: file={}, sr={}",
            spec.sample_rate,
            params.sr
        ));
    }

    let ch = params.out_ch as usize;
    let block_frames = 1024usize;
    let mut buf_f32: Vec<f32> = Vec::with_capacity(block_frames * ch);

    match spec.sample_format {
        hound::SampleFormat::Int => {
            let mut it = reader.samples::<i16>();
            loop {
                if stop_flag.load(Ordering::Relaxed) {
                    break;
                }
                buf_f32.clear();
                for _ in 0..(block_frames * ch) {
                    match it.next() {
                        Some(Ok(s)) => buf_f32.push(convert::i16_to_f32(s)),
                        _ => {
                            stop_flag.store(true, Ordering::Relaxed);
                            break;
                        }
                    }
                }
                if buf_f32.is_empty() {
                    thread::sleep(Duration::from_millis(5));
                    continue;
                }
                let frames = buf_f32.len() / ch;
                let mut off = 0usize;
                while off < buf_f32.len() && !stop_flag.load(Ordering::Relaxed) {
                    let pushed_samples = bus_out
                        .push_samples_partial_nonblocking(&buf_f32[off..], ch)
                        .unwrap_or(0);
                    if pushed_samples == 0 {
                        thread::sleep(Duration::from_millis(2));
                    } else {
                        off += pushed_samples;
                    }
                }
                metrics.frames_out.fetch_add(frames as u64, Ordering::Relaxed);
                thread::sleep(Duration::from_millis(5));
            }
        }
        hound::SampleFormat::Float => {
            let mut it = reader.samples::<f32>();
            loop {
                if stop_flag.load(Ordering::Relaxed) {
                    break;
                }
                buf_f32.clear();
                for _ in 0..(block_frames * ch) {
                    match it.next() {
                        Some(Ok(s)) => buf_f32.push(s),
                        _ => {
                            stop_flag.store(true, Ordering::Relaxed);
                            break;
                        }
                    }
                }
                if buf_f32.is_empty() {
                    thread::sleep(Duration::from_millis(5));
                    continue;
                }
                let frames = buf_f32.len() / ch;
                let mut off = 0usize;
                while off < buf_f32.len() && !stop_flag.load(Ordering::Relaxed) {
                    let pushed_samples = bus_out
                        .push_samples_partial_nonblocking(&buf_f32[off..], ch)
                        .unwrap_or(0);
                    if pushed_samples == 0 {
                        thread::sleep(Duration::from_millis(2));
                    } else {
                        off += pushed_samples;
                    }
                }
                metrics.frames_out.fetch_add(frames as u64, Ordering::Relaxed);
                thread::sleep(Duration::from_millis(5));
            }
        }
    }

    Ok(())
}

