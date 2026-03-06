use crate::backends::{
    AudioBackend, DeviceInfo, DeviceSelector, DeviceSelectorKind, Direction, StreamConfig, StreamHandle,
};
use crate::audio::convert;
use anyhow::{anyhow, Result};
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use std::sync::{Arc, Mutex};

pub struct CpalBackend;

impl CpalBackend {
    pub fn new() -> Self {
        Self
    }

    fn sample_format_rank(fmt: cpal::SampleFormat) -> u8 {
        // Lower is better (prefer higher quality / easier processing).
        match fmt {
            cpal::SampleFormat::F32 => 0,
            cpal::SampleFormat::I16 => 1,
            cpal::SampleFormat::I32 => 2,
            cpal::SampleFormat::U16 => 3,
            cpal::SampleFormat::U8 => 4,
            _ => 100,
        }
    }

    fn find_host_id(&self, hostapi: &str) -> Result<cpal::HostId> {
        let want = hostapi.to_ascii_lowercase();
        cpal::available_hosts()
            .into_iter()
            .find(|h| h.name().to_ascii_lowercase() == want)
            .ok_or_else(|| anyhow!("CPAL hostapi not found: {hostapi}"))
    }

    fn find_device(
        &self,
        host: &cpal::Host,
        direction: Direction,
        sel: &DeviceSelectorKind,
    ) -> Result<cpal::Device> {
        match sel {
            DeviceSelectorKind::Default => match direction {
                Direction::Input => host
                    .default_input_device()
                    .ok_or_else(|| anyhow!("no default input device for this hostapi")),
                Direction::Output => host
                    .default_output_device()
                    .ok_or_else(|| anyhow!("no default output device for this hostapi")),
            },
            DeviceSelectorKind::NameContains(needle) => {
                let needle_lc = needle.trim().to_ascii_lowercase();
                let mut it: Box<dyn Iterator<Item = cpal::Device>> = match direction {
                    Direction::Input => Box::new(host.input_devices()?),
                    Direction::Output => Box::new(host.output_devices()?),
                };
                let mut avail: Vec<String> = Vec::new();
                while let Some(d) = it.next() {
                    let name = d.name().unwrap_or_else(|_| "<unknown>".to_string());
                    if name.to_ascii_lowercase().contains(&needle_lc) {
                        return Ok(d);
                    }
                    avail.push(name);
                }
                Err(anyhow!("device not found: {needle}. available={avail:?}"))
            }
        }
    }

    fn pick_input_config(&self, device: &cpal::Device, cfg: StreamConfig) -> Result<cpal::SupportedStreamConfig> {
        let mut best: Option<(u8, cpal::SupportedStreamConfigRange)> = None;
        for r in device.supported_input_configs()? {
            if r.channels() != cfg.channels {
                continue;
            }
            let min = r.min_sample_rate().0;
            let max = r.max_sample_rate().0;
            if cfg.sr < min || cfg.sr > max {
                continue;
            }
            let rank = Self::sample_format_rank(r.sample_format());
            match &best {
                None => best = Some((rank, r)),
                Some((best_rank, _)) if rank < *best_rank => best = Some((rank, r)),
                _ => {}
            }
        }
        if let Some((_rank, r)) = best {
            Ok(r.with_sample_rate(cpal::SampleRate(cfg.sr)))
        } else {
            Err(anyhow!("no supported input config for sr/ch"))
        }
    }

    fn pick_output_config(&self, device: &cpal::Device, cfg: StreamConfig) -> Result<cpal::SupportedStreamConfig> {
        let mut best: Option<(u8, cpal::SupportedStreamConfigRange)> = None;
        for r in device.supported_output_configs()? {
            if r.channels() != cfg.channels {
                continue;
            }
            let min = r.min_sample_rate().0;
            let max = r.max_sample_rate().0;
            if cfg.sr < min || cfg.sr > max {
                continue;
            }
            let rank = Self::sample_format_rank(r.sample_format());
            match &best {
                None => best = Some((rank, r)),
                Some((best_rank, _)) if rank < *best_rank => best = Some((rank, r)),
                _ => {}
            }
        }
        if let Some((_rank, r)) = best {
            Ok(r.with_sample_rate(cpal::SampleRate(cfg.sr)))
        } else {
            Err(anyhow!("no supported output config for sr/ch"))
        }
    }
}

struct CpalStreamHandle {
    stream: cpal::Stream,
}

impl StreamHandle for CpalStreamHandle {
    fn start(&mut self) -> Result<()> {
        self.stream.play()?;
        Ok(())
    }

    fn stop(&mut self) -> Result<()> {
        self.stream.pause()?;
        Ok(())
    }
}

pub(crate) fn open_asio_duplex_same_device(
    in_cfg: StreamConfig,
    out_cfg: StreamConfig,
    device_in: &DeviceSelectorKind,
    device_out: &DeviceSelectorKind,
    in_callback: Box<dyn FnMut(&[f32]) + Send>,
    out_callback: Box<dyn FnMut(&mut [f32]) + Send>,
) -> Result<(Box<dyn StreamHandle>, Box<dyn StreamHandle>)> {
    let b = CpalBackend::new();
    let host_id = b.find_host_id("ASIO")?;
    let host = cpal::host_from_id(host_id)?;

    let try_device = |d: &cpal::Device| -> Option<(cpal::SupportedStreamConfig, cpal::SupportedStreamConfig)> {
        let in_supported = b.pick_input_config(d, in_cfg).ok()?;
        let out_supported = b.pick_output_config(d, out_cfg).ok()?;
        Some((in_supported, out_supported))
    };

    let pick_default = || -> Result<(cpal::Device, cpal::SupportedStreamConfig, cpal::SupportedStreamConfig)> {
        if let Some(d) = host.default_output_device() {
            if let Some((in_s, out_s)) = try_device(&d) {
                return Ok((d, in_s, out_s));
            }
        }
        if let Some(d) = host.default_input_device() {
            if let Some((in_s, out_s)) = try_device(&d) {
                return Ok((d, in_s, out_s));
            }
        }
        Err(anyhow!("no default duplex-capable device for ASIO at requested sr/ch"))
    };

    let (dev, in_supported, out_supported) = match (device_in, device_out) {
        (DeviceSelectorKind::Default, DeviceSelectorKind::Default) => pick_default()?,
        (DeviceSelectorKind::NameContains(needle), _) | (_, DeviceSelectorKind::NameContains(needle)) => {
            let needle_lc = needle.trim().to_ascii_lowercase();
            let mut avail: Vec<String> = Vec::new();
            let mut found: Option<(cpal::Device, cpal::SupportedStreamConfig, cpal::SupportedStreamConfig)> = None;
            for d in host.devices()? {
                let name = d.name().unwrap_or_else(|_| "<unknown>".to_string());
                if !name.to_ascii_lowercase().contains(&needle_lc) {
                    avail.push(name);
                    continue;
                }
                if let Some((in_s, out_s)) = try_device(&d) {
                    found = Some((d, in_s, out_s));
                    break;
                }
                avail.push(name);
            }
            found.ok_or_else(|| {
                anyhow!("ASIO duplex device not found/supported for sr/ch. needle={needle}. available={avail:?}")
            })?
        }
    };

    let input = Box::new(CpalStreamHandle {
        stream: build_input_stream(&dev, &in_supported, in_callback)?,
    });
    let output = Box::new(CpalStreamHandle {
        stream: build_output_stream(&dev, &out_supported, out_callback)?,
    });
    Ok((input, output))
}

fn build_input_stream(
    dev: &cpal::Device,
    supported: &cpal::SupportedStreamConfig,
    mut callback: Box<dyn FnMut(&[f32]) + Send>,
) -> Result<cpal::Stream> {
    let sample_format = supported.sample_format();
    let mut scfg = supported.config();
    scfg.buffer_size = cpal::BufferSize::Default;

    let cb = Arc::new(Mutex::new(move |buf: &[f32]| (callback)(buf)));
    let err_fn = move |err| eprintln!("[cpal] input stream error: {err}");

    let stream = match sample_format {
        cpal::SampleFormat::F32 => {
            let cb = cb.clone();
            dev.build_input_stream(
                &scfg,
                move |data: &[f32], _| {
                    if let Ok(mut f) = cb.lock() {
                        f(data);
                    }
                },
                err_fn,
                None,
            )?
        }
        cpal::SampleFormat::I16 => {
            let cb = cb.clone();
            dev.build_input_stream(
                &scfg,
                move |data: &[i16], _| {
                    let mut tmp = Vec::with_capacity(data.len());
                    for &x in data {
                        tmp.push(convert::i16_to_f32(x));
                    }
                    if let Ok(mut f) = cb.lock() {
                        f(&tmp);
                    }
                },
                err_fn,
                None,
            )?
        }
        cpal::SampleFormat::I32 => {
            let cb = cb.clone();
            dev.build_input_stream(
                &scfg,
                move |data: &[i32], _| {
                    let mut tmp = Vec::with_capacity(data.len());
                    for &x in data {
                        tmp.push(convert::i32_to_f32(x));
                    }
                    if let Ok(mut f) = cb.lock() {
                        f(&tmp);
                    }
                },
                err_fn,
                None,
            )?
        }
        cpal::SampleFormat::U16 => {
            let cb = cb.clone();
            dev.build_input_stream(
                &scfg,
                move |data: &[u16], _| {
                    let mut tmp = Vec::with_capacity(data.len());
                    for &x in data {
                        tmp.push(convert::u16_to_f32(x));
                    }
                    if let Ok(mut f) = cb.lock() {
                        f(&tmp);
                    }
                },
                err_fn,
                None,
            )?
        }
        cpal::SampleFormat::U8 => {
            let cb = cb.clone();
            dev.build_input_stream(
                &scfg,
                move |data: &[u8], _| {
                    let mut tmp = Vec::with_capacity(data.len());
                    for &x in data {
                        tmp.push(convert::u8_to_f32(x));
                    }
                    if let Ok(mut f) = cb.lock() {
                        f(&tmp);
                    }
                },
                err_fn,
                None,
            )?
        }
        other => return Err(anyhow!("unsupported input sample format: {other:?}")),
    };
    Ok(stream)
}

fn build_output_stream(
    dev: &cpal::Device,
    supported: &cpal::SupportedStreamConfig,
    mut callback: Box<dyn FnMut(&mut [f32]) + Send>,
) -> Result<cpal::Stream> {
    let sample_format = supported.sample_format();
    let mut scfg = supported.config();
    scfg.buffer_size = cpal::BufferSize::Default;

    let cb = Arc::new(Mutex::new(move |buf: &mut [f32]| (callback)(buf)));
    let err_fn = move |err| eprintln!("[cpal] output stream error: {err}");

    let stream = match sample_format {
        cpal::SampleFormat::F32 => {
            let cb = cb.clone();
            dev.build_output_stream(
                &scfg,
                move |out: &mut [f32], _| {
                    if let Ok(mut f) = cb.lock() {
                        f(out);
                    }
                },
                err_fn,
                None,
            )?
        }
        cpal::SampleFormat::I16 => {
            let cb = cb.clone();
            dev.build_output_stream(
                &scfg,
                move |out: &mut [i16], _| {
                    let mut tmp = vec![0.0f32; out.len()];
                    if let Ok(mut f) = cb.lock() {
                        f(&mut tmp);
                    }
                    for (dst, &x) in out.iter_mut().zip(tmp.iter()) {
                        *dst = convert::f32_to_i16(x);
                    }
                },
                err_fn,
                None,
            )?
        }
        cpal::SampleFormat::I32 => {
            let cb = cb.clone();
            dev.build_output_stream(
                &scfg,
                move |out: &mut [i32], _| {
                    let mut tmp = vec![0.0f32; out.len()];
                    if let Ok(mut f) = cb.lock() {
                        f(&mut tmp);
                    }
                    for (dst, &x) in out.iter_mut().zip(tmp.iter()) {
                        *dst = convert::f32_to_i32(x);
                    }
                },
                err_fn,
                None,
            )?
        }
        cpal::SampleFormat::U8 => {
            let cb = cb.clone();
            dev.build_output_stream(
                &scfg,
                move |out: &mut [u8], _| {
                    let mut tmp = vec![0.0f32; out.len()];
                    if let Ok(mut f) = cb.lock() {
                        f(&mut tmp);
                    }
                    for (dst, &x) in out.iter_mut().zip(tmp.iter()) {
                        *dst = convert::f32_to_u8(x);
                    }
                },
                err_fn,
                None,
            )?
        }
        other => return Err(anyhow!("unsupported output sample format: {other:?}")),
    };
    Ok(stream)
}

impl AudioBackend for CpalBackend {
    fn name(&self) -> &'static str {
        "cpal"
    }

    fn list_hostapis(&self) -> Vec<String> {
        cpal::available_hosts()
            .into_iter()
            .map(|h| h.name().to_string())
            .collect()
    }

    fn list_devices(&self, hostapi: &str, direction: Direction) -> Result<Vec<DeviceInfo>> {
        let host_id = self.find_host_id(hostapi)?;
        let host = cpal::host_from_id(host_id)?;

        let iter: Box<dyn Iterator<Item = cpal::Device>> = match direction {
            Direction::Input => Box::new(host.input_devices()?),
            Direction::Output => Box::new(host.output_devices()?),
        };

        let mut out = Vec::new();
        for d in iter {
            let name = d.name().unwrap_or_else(|_| "<unknown>".to_string());
            let (max_in, max_out) = match direction {
                Direction::Input => (cfg_max_channels_input(&d), 0),
                Direction::Output => (0, cfg_max_channels_output(&d)),
            };
            out.push(DeviceInfo {
                name,
                max_input_channels: max_in,
                max_output_channels: max_out,
                default_sr: 48_000,
            });
        }
        Ok(out)
    }

    fn open_input(
        &self,
        cfg: StreamConfig,
        device: DeviceSelector,
        mut callback: Box<dyn FnMut(&[f32]) + Send>,
    ) -> Result<Box<dyn StreamHandle>> {
        let host_id = self.find_host_id(&device.hostapi)?;
        let host = cpal::host_from_id(host_id)?;
        let dev = self.find_device(&host, Direction::Input, &device.selector)?;

        let supported = self.pick_input_config(&dev, cfg)?;
        let sample_format = supported.sample_format();
        let mut scfg = supported.config();
        scfg.buffer_size = cpal::BufferSize::Default;

        let cb = Arc::new(Mutex::new(move |buf: &[f32]| (callback)(buf)));
        let err_fn = move |err| eprintln!("[cpal] input stream error: {err}");

        let stream = match sample_format {
            cpal::SampleFormat::F32 => {
                let cb = cb.clone();
                dev.build_input_stream(
                    &scfg,
                    move |data: &[f32], _| {
                        if let Ok(mut f) = cb.lock() {
                            f(data);
                        }
                    },
                    err_fn,
                    None,
                )?
            }
            cpal::SampleFormat::I16 => {
                let cb = cb.clone();
                dev.build_input_stream(
                    &scfg,
                    move |data: &[i16], _| {
                        let mut tmp = Vec::with_capacity(data.len());
                        for &x in data {
                            tmp.push(convert::i16_to_f32(x));
                        }
                        if let Ok(mut f) = cb.lock() {
                            f(&tmp);
                        }
                    },
                    err_fn,
                    None,
                )?
            }
            cpal::SampleFormat::I32 => {
                let cb = cb.clone();
                dev.build_input_stream(
                    &scfg,
                    move |data: &[i32], _| {
                        let mut tmp = Vec::with_capacity(data.len());
                        for &x in data {
                            tmp.push(convert::i32_to_f32(x));
                        }
                        if let Ok(mut f) = cb.lock() {
                            f(&tmp);
                        }
                    },
                    err_fn,
                    None,
                )?
            }
            cpal::SampleFormat::U16 => {
                let cb = cb.clone();
                dev.build_input_stream(
                    &scfg,
                    move |data: &[u16], _| {
                        let mut tmp = Vec::with_capacity(data.len());
                        for &x in data {
                            tmp.push(convert::u16_to_f32(x));
                        }
                        if let Ok(mut f) = cb.lock() {
                            f(&tmp);
                        }
                    },
                    err_fn,
                    None,
                )?
            }
            cpal::SampleFormat::U8 => {
                let cb = cb.clone();
                dev.build_input_stream(
                    &scfg,
                    move |data: &[u8], _| {
                        let mut tmp = Vec::with_capacity(data.len());
                        for &x in data {
                            tmp.push(convert::u8_to_f32(x));
                        }
                        if let Ok(mut f) = cb.lock() {
                            f(&tmp);
                        }
                    },
                    err_fn,
                    None,
                )?
            }
            other => return Err(anyhow!("unsupported input sample format: {other:?}")),
        };

        let handle = CpalStreamHandle { stream };
        Ok(Box::new(handle))
    }

    fn open_output(
        &self,
        cfg: StreamConfig,
        device: DeviceSelector,
        mut callback: Box<dyn FnMut(&mut [f32]) + Send>,
    ) -> Result<Box<dyn StreamHandle>> {
        let host_id = self.find_host_id(&device.hostapi)?;
        let host = cpal::host_from_id(host_id)?;
        let dev = self.find_device(&host, Direction::Output, &device.selector)?;

        let supported = self.pick_output_config(&dev, cfg)?;
        let sample_format = supported.sample_format();
        let mut scfg = supported.config();
        scfg.buffer_size = cpal::BufferSize::Default;

        let cb = Arc::new(Mutex::new(move |buf: &mut [f32]| (callback)(buf)));
        let err_fn = move |err| eprintln!("[cpal] output stream error: {err}");

        let stream = match sample_format {
            cpal::SampleFormat::F32 => {
                let cb = cb.clone();
                dev.build_output_stream(
                    &scfg,
                    move |out: &mut [f32], _| {
                        if let Ok(mut f) = cb.lock() {
                            f(out);
                        }
                    },
                    err_fn,
                    None,
                )?
            }
            cpal::SampleFormat::I16 => {
                let cb = cb.clone();
                dev.build_output_stream(
                    &scfg,
                    move |out: &mut [i16], _| {
                        let mut tmp = vec![0.0f32; out.len()];
                        if let Ok(mut f) = cb.lock() {
                            f(&mut tmp);
                        }
                        for (dst, &x) in out.iter_mut().zip(tmp.iter()) {
                            *dst = convert::f32_to_i16(x);
                        }
                    },
                    err_fn,
                    None,
                )?
            }
            cpal::SampleFormat::I32 => {
                let cb = cb.clone();
                dev.build_output_stream(
                    &scfg,
                    move |out: &mut [i32], _| {
                        let mut tmp = vec![0.0f32; out.len()];
                        if let Ok(mut f) = cb.lock() {
                            f(&mut tmp);
                        }
                        for (dst, &x) in out.iter_mut().zip(tmp.iter()) {
                            *dst = convert::f32_to_i32(x);
                        }
                    },
                    err_fn,
                    None,
                )?
            }
            cpal::SampleFormat::U8 => {
                let cb = cb.clone();
                dev.build_output_stream(
                    &scfg,
                    move |out: &mut [u8], _| {
                        let mut tmp = vec![0.0f32; out.len()];
                        if let Ok(mut f) = cb.lock() {
                            f(&mut tmp);
                        }
                        for (dst, &x) in out.iter_mut().zip(tmp.iter()) {
                            *dst = convert::f32_to_u8(x);
                        }
                    },
                    err_fn,
                    None,
                )?
            }
            other => return Err(anyhow!("unsupported output sample format: {other:?}")),
        };

        let handle = CpalStreamHandle { stream };
        Ok(Box::new(handle))
    }
}

fn cfg_max_channels_input(device: &cpal::Device) -> u16 {
    device
        .supported_input_configs()
        .ok()
        .and_then(|mut it| it.map(|c| c.channels()).max())
        .unwrap_or(0)
}

fn cfg_max_channels_output(device: &cpal::Device) -> u16 {
    device
        .supported_output_configs()
        .ok()
        .and_then(|mut it| it.map(|c| c.channels()).max())
        .unwrap_or(0)
}

