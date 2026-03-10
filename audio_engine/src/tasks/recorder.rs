use crate::audio::convert;
use crate::audio::ring::AudioRing;
use crate::engine::metrics::Metrics;
use crate::engine::routes::SessionParams;
use anyhow::{anyhow, Result};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

pub fn spawn_wav_recorder(
    mut bus_in: AudioRing,
    stop_flag: Arc<AtomicBool>,
    params: SessionParams,
    metrics: Arc<Metrics>,
) -> Result<thread::JoinHandle<()>> {
    if params.path.is_empty() {
        return Err(anyhow!("recorder requires a non-empty path"));
    }
    let handle = thread::spawn(move || {
        let _ = wav_recorder_loop(&mut bus_in, stop_flag, params, metrics);
    });
    Ok(handle)
}

fn wav_recorder_loop(
    bus_in: &mut AudioRing,
    stop_flag: Arc<AtomicBool>,
    params: SessionParams,
    metrics: Arc<Metrics>,
) -> Result<()> {
    let sr = params.sr;
    let ch = params.in_ch;
    let rotate_s = params.rotate_s;

    // For short recordings (mode="record"), enforce an exact frame count so the WAV length
    // matches the client-requested duration even if the audio callback size overshoots.
    let stop_after_frames: Option<u64> = {
        let m = params.mode.to_ascii_lowercase();
        if (m == "record" || m == "record_short") && params.duration_s > 0.0 {
            // Avoid truncation due to float representation (e.g. 4.999999999 -> 239999).
            Some((params.duration_s * (sr as f64)).round() as u64)
        } else {
            None
        }
    };
    let mut total_frames_written: u64 = 0;

    let mut seg_idx = 0usize;
    let mut frames_in_seg: u64 = 0;
    let seg_limit_frames: u64 = if rotate_s > 0.0 {
        (rotate_s * (sr as f64)).round() as u64
    } else {
        0
    };

    let mut writer = open_writer(&params.path, seg_idx, ch, sr)?;

    let block_frames = 1024usize;
    let mut tmp = vec![0.0f32; block_frames * (ch as usize)];

    loop {
        let got = bus_in.pop_samples(&mut tmp)?;
        if got == 0 {
            // If a stop was requested, keep draining any remaining buffered audio
            // before finalizing the WAV. This prevents dropping the last callback-sized
            // chunk (often ~10ms on Windows audio stacks).
            if stop_flag.load(Ordering::Relaxed) {
                break;
            }
            thread::sleep(Duration::from_millis(5));
            continue;
        }

        let got_frames = (got / (ch as usize)) as u64;
        let frames_to_write = match stop_after_frames {
            Some(limit) if total_frames_written < limit => (limit - total_frames_written).min(got_frames),
            Some(_limit) => 0,
            None => got_frames,
        };
        if frames_to_write == 0 {
            stop_flag.store(true, Ordering::Relaxed);
            break;
        }

        let samples_to_write = (frames_to_write as usize) * (ch as usize);
        let pcm16_bytes = convert::f32_to_pcm16_bytes_interleaved(&tmp[..samples_to_write]);
        for chunk in pcm16_bytes.chunks_exact(2) {
            let s = i16::from_le_bytes([chunk[0], chunk[1]]);
            if writer.write_sample(s).is_err() {
                break;
            }
        }

        metrics.wav_frames.fetch_add(frames_to_write, Ordering::Relaxed);
        total_frames_written = total_frames_written.saturating_add(frames_to_write);
        frames_in_seg += frames_to_write;

        if seg_limit_frames > 0 && frames_in_seg >= seg_limit_frames {
            let _ = writer.finalize();
            seg_idx += 1;
            frames_in_seg = 0;
            writer = open_writer(&params.path, seg_idx, ch, sr)?;
        }

        if let Some(limit) = stop_after_frames {
            if total_frames_written >= limit {
                stop_flag.store(true, Ordering::Relaxed);
                break;
            }
        }
    }

    let _ = writer.finalize();
    Ok(())
}

fn open_writer(path: &str, seg_idx: usize, ch: u16, sr: u32) -> Result<hound::WavWriter<std::io::BufWriter<std::fs::File>>> {
    let out_path = if seg_idx == 0 {
        PathBuf::from(path)
    } else {
        rotated_path(path, seg_idx)
    };

    if let Some(parent) = out_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }

    let spec = hound::WavSpec {
        channels: ch,
        sample_rate: sr,
        bits_per_sample: 16,
        sample_format: hound::SampleFormat::Int,
    };

    Ok(hound::WavWriter::create(out_path, spec)?)
}

fn rotated_path(base: &str, seg_idx: usize) -> PathBuf {
    let p = Path::new(base);
    let stem = p.file_stem().and_then(|s| s.to_str()).unwrap_or("record");
    let ext = p.extension().and_then(|s| s.to_str()).unwrap_or("wav");
    let parent = p.parent().unwrap_or_else(|| Path::new(""));
    parent.join(format!("{stem}_{seg_idx:05}.{ext}"))
}


