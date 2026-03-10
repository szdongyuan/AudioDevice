use serde::Serialize;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

#[derive(Debug)]
pub struct Metrics {
    start_t: Instant,
    pub frames_in: AtomicU64,
    pub frames_out: AtomicU64,
    pub underruns: AtomicU64,
    pub overruns: AtomicU64,
    pub capture_frames: AtomicU64,
    pub wav_frames: AtomicU64,
}

impl Metrics {
    pub fn new() -> Self {
        Self {
            start_t: Instant::now(),
            frames_in: AtomicU64::new(0),
            frames_out: AtomicU64::new(0),
            underruns: AtomicU64::new(0),
            overruns: AtomicU64::new(0),
            capture_frames: AtomicU64::new(0),
            wav_frames: AtomicU64::new(0),
        }
    }

    pub fn snapshot(&self) -> MetricsSnapshot {
        MetricsSnapshot {
            uptime_s: self.start_t.elapsed().as_secs_f64(),
            frames_in: self.frames_in.load(Ordering::Relaxed),
            frames_out: self.frames_out.load(Ordering::Relaxed),
            underruns: self.underruns.load(Ordering::Relaxed),
            overruns: self.overruns.load(Ordering::Relaxed),
            capture_frames: self.capture_frames.load(Ordering::Relaxed),
            wav_frames: self.wav_frames.load(Ordering::Relaxed),
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct MetricsSnapshot {
    pub uptime_s: f64,
    pub frames_in: u64,
    pub frames_out: u64,
    pub underruns: u64,
    pub overruns: u64,
    pub capture_frames: u64,
    pub wav_frames: u64,
}

