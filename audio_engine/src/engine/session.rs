use crate::audio::convert;
use crate::audio::ring::AudioRing;
use crate::backends::{AudioBackend, DeviceSelector, DeviceSelectorKind, StreamConfig, StreamHandle};
use crate::backends::cpal_backend;
use crate::engine::metrics::Metrics;
use crate::engine::routes::{CaptureReadReply, SessionParams};
use crate::tasks::recorder;
use crate::tasks::player;
use anyhow::{anyhow, Result};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionMode {
    RecordShort,
    RecordLong,
    MonitorRecord,
    Play,
    PlayRec,
}

impl SessionMode {
    pub fn from_str(s: &str) -> Result<Self> {
        match s.to_ascii_lowercase().as_str() {
            "record" | "record_short" => Ok(Self::RecordShort),
            "record_long" | "long_record" => Ok(Self::RecordLong),
            "monitor_record" | "monitorrec" => Ok(Self::MonitorRecord),
            "play" => Ok(Self::Play),
            "playrec" => Ok(Self::PlayRec),
            other => Err(anyhow!("unknown mode: {other}")),
        }
    }
}

struct SessionInner {
    params: SessionParams,
    mode: SessionMode,
    backend: Box<dyn AudioBackend>,

    input: Option<Box<dyn StreamHandle>>,
    output: Option<Box<dyn StreamHandle>>,

    bus_in: AudioRing,
    bus_out: AudioRing,
    capture: AudioRing,

    metrics: Arc<Metrics>,
    stop_flag: Arc<AtomicBool>,
    play_finished: Arc<AtomicBool>,
    started_at: Instant,
}

pub struct Session {
    inner: Arc<Mutex<SessionInner>>,
    tasks: Vec<thread::JoinHandle<()>>,
}

impl Session {
    pub fn start(
        backend: Box<dyn AudioBackend>,
        mut params: SessionParams,
        metrics: Arc<Metrics>,
    ) -> Result<Self> {
        let mode = SessionMode::from_str(&params.mode)?;
        if matches!(mode, SessionMode::RecordLong) && params.rotate_s <= 0.0 {
            params.rotate_s = 300.0;
        }
        let rb_seconds = if params.rb_seconds == 0 { 2 } else { params.rb_seconds };

        let bus_in = AudioRing::new(params.sr, params.in_ch, rb_seconds)?;
        let bus_out = AudioRing::new(params.sr, params.out_ch, rb_seconds)?;
        let capture = AudioRing::new(params.sr, params.in_ch, rb_seconds)?;

        let stop_flag = Arc::new(AtomicBool::new(false));
        let play_finished = Arc::new(AtomicBool::new(false));

        let mut inner = SessionInner {
            params: params.clone(),
            mode,
            backend,
            input: None,
            output: None,
            bus_in,
            bus_out,
            capture,
            metrics,
            stop_flag: stop_flag.clone(),
            play_finished: play_finished.clone(),
            started_at: Instant::now(), // temporary, updated after streams are started
        };

        inner.open_streams()?;
        // Start timing after streams are successfully started.
        let started_at = Instant::now();
        inner.started_at = started_at;
        let inner = Arc::new(Mutex::new(inner));
        let mut tasks = Vec::new();

        // WAV recorder for modes that require it.
        {
            let s = inner.lock().unwrap();
            let need_recorder = matches!(
                s.mode,
                SessionMode::RecordShort | SessionMode::RecordLong | SessionMode::MonitorRecord | SessionMode::PlayRec
            ) && !s.params.path.is_empty();
            if need_recorder {
                tasks.push(recorder::spawn_wav_recorder(
                    s.bus_in.clone_for_callback(),
                    s.stop_flag.clone(),
                    s.params.clone(),
                    s.metrics.clone(),
                )?);
            }
        }

        // Optional file playback.
        {
            let s = inner.lock().unwrap();
            let need_file_play = !s.params.play_path.is_empty()
                && matches!(s.mode, SessionMode::Play | SessionMode::PlayRec);
            if need_file_play {
                tasks.push(player::spawn_wav_player(
                    s.bus_out.clone_for_callback(),
                    s.stop_flag.clone(),
                    s.params.clone(),
                    s.metrics.clone(),
                )?);
            }
        }

        // Auto-stop is only used for short, input-only recording.
        // RecordShort auto-stop is handled in the input callback based on frame count.

        // Ensure streams are dropped when stop_flag becomes true (e.g. play drain).
        tasks.push(spawn_stop_watcher(inner.clone())?);

        Ok(Self { inner, tasks })
    }

    pub fn stop(&mut self) -> Result<()> {
        {
            let mut inner = self.inner.lock().unwrap();
            inner.stop_flag.store(true, Ordering::Relaxed);
            inner.input.take();
            inner.output.take();
        }

        while let Some(h) = self.tasks.pop() {
            let _ = h.join();
        }
        Ok(())
    }

    pub fn capture_read(&mut self, max_frames: usize) -> Result<CaptureReadReply> {
        let mut inner = self.inner.lock().unwrap();

        inner.maybe_auto_stop();

        let frames = max_frames.max(1);
        let mut tmp = vec![0.0f32; frames * (inner.capture.channels() as usize)];
        let got_samples = inner.capture.pop_samples(&mut tmp)?;
        let got_frames = got_samples / (inner.capture.channels() as usize);

        tmp.truncate(got_samples);
        let pcm16 = convert::f32_to_pcm16_bytes_interleaved(&tmp);
        let b64 = convert::base64_encode(&pcm16);

        let eof = inner.stop_flag.load(Ordering::Relaxed) && got_samples == 0;
        Ok(CaptureReadReply {
            pcm16_b64: b64,
            frames: got_frames,
            channels: inner.capture.channels(),
            sr: inner.capture.sr(),
            eof,
        })
    }

    pub fn play_write_pcm16_base64(&mut self, pcm16_b64: &str) -> Result<usize> {
        let mut inner = self.inner.lock().unwrap();
        if inner.params.out_ch == 0 {
            return Err(anyhow!("out_ch must be > 0 for play_write"));
        }

        let bytes = convert::base64_decode(pcm16_b64)?;
        let f32s = convert::pcm16_bytes_to_f32_interleaved(&bytes)?;

        let ch = inner.params.out_ch as usize;
        let pushed_samples = inner.bus_out.push_samples_partial_nonblocking(&f32s, ch)?;
        Ok(pushed_samples / ch)
    }

    pub fn play_finish(&mut self) -> Result<()> {
        let mut inner = self.inner.lock().unwrap();
        inner.play_finished.store(true, Ordering::Relaxed);
        Ok(())
    }

    pub fn is_finished(&self) -> bool {
        let inner = self.inner.lock().unwrap();
        let stopped = inner.stop_flag.load(Ordering::Relaxed);
        if !stopped {
            return false;
        }

        // If we are returning audio to the client, keep the session alive until capture is drained.
        let capture_drained = if inner.params.return_audio {
            inner.capture.is_empty()
        } else {
            true
        };

        capture_drained && inner.input.is_none() && inner.output.is_none()
    }
}

fn spawn_auto_stop_if_needed(
    inner: Arc<Mutex<SessionInner>>,
    started_at: Instant,
    duration_s: f64,
) -> Result<thread::JoinHandle<()>> {
    let h = thread::spawn(move || {
        let dur = Duration::from_secs_f64(duration_s.max(0.0));
        while Instant::now().duration_since(started_at) < dur {
            let stop = inner.lock().unwrap().stop_flag.load(Ordering::Relaxed);
            if stop {
                return;
            }
            thread::sleep(Duration::from_millis(20));
        }
        let mut s = inner.lock().unwrap();
        s.stop_flag.store(true, Ordering::Relaxed);
        s.input.take();
        s.output.take();
    });
    Ok(h)
}

fn spawn_stop_watcher(inner: Arc<Mutex<SessionInner>>) -> Result<thread::JoinHandle<()>> {
    let h = thread::spawn(move || loop {
        {
            let mut s = inner.lock().unwrap();
            if s.stop_flag.load(Ordering::Relaxed) {
                s.input.take();
                s.output.take();
                return;
            }
        }
        thread::sleep(Duration::from_millis(20));
    });
    Ok(h)
}

impl SessionInner {
    fn open_streams(&mut self) -> Result<()> {
        let in_cfg = StreamConfig {
            sr: self.params.sr,
            channels: self.params.in_ch,
        };
        let out_cfg = StreamConfig {
            sr: self.params.sr,
            channels: self.params.out_ch,
        };

        let hostapi = self.params.hostapi.clone();
        let device_in = DeviceSelector {
            hostapi: hostapi.clone(),
            selector: if self.params.device_in.is_empty() {
                DeviceSelectorKind::Default
            } else {
                DeviceSelectorKind::NameContains(self.params.device_in.clone())
            },
        };
        let device_out = DeviceSelector {
            hostapi: hostapi.clone(),
            selector: if self.params.device_out.is_empty() {
                DeviceSelectorKind::Default
            } else {
                DeviceSelectorKind::NameContains(self.params.device_out.clone())
            },
        };

        let stop_flag = self.stop_flag.clone();
        let metrics = self.metrics.clone();

        let need_in = matches!(
            self.mode,
            SessionMode::RecordShort | SessionMode::RecordLong | SessionMode::MonitorRecord | SessionMode::PlayRec
        );
        let need_out = matches!(
            self.mode,
            SessionMode::MonitorRecord | SessionMode::Play | SessionMode::PlayRec
        ) && self.params.out_ch > 0;

        if need_in && self.params.in_ch == 0 {
            return Err(anyhow!("in_ch must be > 0 for input modes"));
        }

        if need_out && self.params.out_ch == 0 {
            return Err(anyhow!("out_ch must be > 0 for output modes"));
        }

        // Some ASIO drivers behave poorly when input/output are opened as separate streams.
        // For cpal+ASIO, prefer opening both streams on the same device when we're in a
        // duplex mode (playrec or monitor_record). Allow one side to be "default"/empty,
        // in which case we still try to pick a duplex-capable ASIO device.
        let use_cpal_asio_duplex_same_device = self.backend.name() == "cpal"
            && hostapi.eq_ignore_ascii_case("ASIO")
            && matches!(self.mode, SessionMode::PlayRec | SessionMode::MonitorRecord)
            && need_in
            && need_out
            && (self.params.device_in == self.params.device_out
                || self.params.device_in.is_empty()
                || self.params.device_out.is_empty());

        let mut in_cb_opt: Option<Box<dyn FnMut(&[f32]) + Send>> = None;
        let mut out_cb_opt: Option<Box<dyn FnMut(&mut [f32]) + Send>> = None;

        if need_in {
            let mut bus_in = self.bus_in.clone_for_callback();
            let mut capture = self.capture.clone_for_callback();
            let return_audio = self.params.return_audio;
            let is_monitor = matches!(self.mode, SessionMode::MonitorRecord);
            let mut bus_out_for_monitor = self.bus_out.clone_for_callback();
            let in_ch_usize = in_cfg.channels as usize;
            let out_ch_usize = out_cfg.channels as usize;
            let mut monitor_tmp: Vec<f32> = Vec::new();
            let need_wav_recorder = !self.params.path.is_empty();

            let stop_flag_in = stop_flag.clone();
            let metrics_in = metrics.clone();
            let stop_after_frames: Option<u64> =
                if matches!(self.mode, SessionMode::RecordShort) && self.params.duration_s > 0.0 {
                    // Avoid truncation due to float representation (e.g. 4.999999999 -> 239999).
                    Some((self.params.duration_s * (in_cfg.sr as f64)).round() as u64)
                } else {
                    None
                };
            let mut frames_seen: u64 = 0;
            let cb = Box::new(move |data: &[f32]| {
                if stop_flag_in.load(Ordering::Relaxed) {
                    return;
                }
                let frames = data.len() / (in_cfg.channels as usize);
                metrics_in
                    .frames_in
                    .fetch_add(frames as u64, Ordering::Relaxed);
                // When a WAV recorder is active, base short-record stopping on *accepted* frames.
                // This avoids stopping early if the ring buffer briefly fills (which would make
                // the resulting WAV slightly shorter than requested).
                let pushed_samples_bus = if need_wav_recorder {
                    match bus_in.push_samples_partial_nonblocking(data, in_ch_usize) {
                        Ok(n) => {
                            if n < data.len() {
                                metrics_in.overruns.fetch_add(1, Ordering::Relaxed);
                            }
                            n
                        }
                        Err(_) => 0,
                    }
                } else {
                    let _ = bus_in.push_samples_nonblocking(data, &metrics_in);
                    data.len()
                };
                if return_audio {
                    if need_wav_recorder {
                        let _ = capture
                            .push_samples_partial_nonblocking(data, in_ch_usize)
                            .map(|n| {
                                if n < data.len() {
                                    metrics_in.overruns.fetch_add(1, Ordering::Relaxed);
                                }
                                n
                            });
                    } else {
                        let _ = capture.push_samples_nonblocking(data, &metrics_in);
                    }
                    metrics_in
                        .capture_frames
                        .fetch_add(frames as u64, Ordering::Relaxed);
                }

                if is_monitor && out_ch_usize > 0 {
                    if in_ch_usize == out_ch_usize {
                        let _ = bus_out_for_monitor.push_samples_nonblocking(data, &metrics_in);
                    } else {
                        // Lightweight channel mapping for monitoring (no resampling).
                        // - 1 -> N: duplicate
                        // - N -> 1: average
                        // - N -> M: take first min(N,M), repeat last if M>N
                        monitor_tmp.clear();
                        monitor_tmp.reserve(frames * out_ch_usize);
                        for f in 0..frames {
                            let base = f * in_ch_usize;
                            let frame_in = &data[base..base + in_ch_usize];
                            if out_ch_usize == 1 {
                                let v = if in_ch_usize == 1 {
                                    frame_in[0]
                                } else {
                                    let mut acc = 0.0f32;
                                    for &x in frame_in {
                                        acc += x;
                                    }
                                    acc / (in_ch_usize as f32)
                                };
                                monitor_tmp.push(v);
                            } else if in_ch_usize == 1 {
                                let v = frame_in[0];
                                for _ in 0..out_ch_usize {
                                    monitor_tmp.push(v);
                                }
                            } else {
                                let last = frame_in[in_ch_usize - 1];
                                for oc in 0..out_ch_usize {
                                    let v = if oc < in_ch_usize { frame_in[oc] } else { last };
                                    monitor_tmp.push(v);
                                }
                            }
                        }
                        let _ = bus_out_for_monitor.push_samples_nonblocking(&monitor_tmp, &metrics_in);
                    }
                }

                if let Some(target) = stop_after_frames {
                    let frames_for_stop = if need_wav_recorder {
                        (pushed_samples_bus / in_ch_usize) as u64
                    } else {
                        frames as u64
                    };
                    frames_seen = frames_seen.saturating_add(frames_for_stop);
                    if frames_seen >= target {
                        stop_flag_in.store(true, Ordering::Relaxed);
                    }
                }
            });
            in_cb_opt = Some(cb);
        }

        if need_out {
            let mut bus_out = self.bus_out.clone_for_callback();
            let stop_flag_out = stop_flag.clone();
            let metrics_out = metrics.clone();
            let play_finished = self.play_finished.clone();
            let mut empty_count: u32 = 0;
            let cb = Box::new(move |out: &mut [f32]| {
                if stop_flag_out.load(Ordering::Relaxed) {
                    for x in out.iter_mut() {
                        *x = 0.0;
                    }
                    return;
                }
                let _ = bus_out.pop_into_slice_nonblocking(out, &metrics_out);
                let frames = out.len() / (out_cfg.channels as usize);
                metrics_out
                    .frames_out
                    .fetch_add(frames as u64, Ordering::Relaxed);

                // Drain-stop logic for play/playrec: once play_finish was received and the output
                // queue stays empty for a few callbacks, stop the session.
                if play_finished.load(Ordering::Relaxed) {
                    if bus_out.is_empty() {
                        empty_count = empty_count.saturating_add(1);
                    } else {
                        empty_count = 0;
                    }
                    if empty_count >= 20 {
                        stop_flag_out.store(true, Ordering::Relaxed);
                    }
                }
            });

            out_cb_opt = Some(cb);
        }

        if use_cpal_asio_duplex_same_device {
            let in_cb = in_cb_opt.take().ok_or_else(|| anyhow!("internal: missing input callback"))?;
            let out_cb = out_cb_opt.take().ok_or_else(|| anyhow!("internal: missing output callback"))?;
            let (inp, out) = cpal_backend::open_asio_duplex_same_device(
                in_cfg,
                out_cfg,
                &device_in.selector,
                &device_out.selector,
                in_cb,
                out_cb,
            )?;
            self.input = Some(inp);
            self.output = Some(out);
        } else {
            if need_in {
                let cb = in_cb_opt.take().ok_or_else(|| anyhow!("internal: missing input callback"))?;
                let s = self.backend.open_input(in_cfg, device_in, cb)?;
                self.input = Some(s);
            }
            if need_out {
                let cb = out_cb_opt.take().ok_or_else(|| anyhow!("internal: missing output callback"))?;
                let s = self.backend.open_output(out_cfg, device_out, cb)?;
                self.output = Some(s);
            }
        }

        // Start streams after both are created (better duplex behavior on some drivers).
        if let Some(inp) = self.input.as_mut() {
            inp.start()?;
        }
        if let Some(out) = self.output.as_mut() {
            out.start()?;
        }

        Ok(())
    }

    fn maybe_auto_stop(&mut self) {
        if self.stop_flag.load(Ordering::Relaxed) {
            self.input.take();
            self.output.take();
        }
    }
}

