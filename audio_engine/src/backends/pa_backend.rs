use crate::backends::{
    AudioBackend, DeviceInfo, DeviceSelector, DeviceSelectorKind, Direction, StreamConfig, StreamHandle,
};
use anyhow::{anyhow, Result};
use portaudio as pa;

pub struct PortAudioBackend {
    pa: pa::PortAudio,
}

impl PortAudioBackend {
    pub fn new() -> Result<Self> {
        let pa = pa::PortAudio::new().map_err(|e| anyhow!("PortAudio init failed: {e:?}"))?;
        Ok(Self { pa })
    }

    fn hostapi_type_from_name(hostapi: &str) -> Result<pa::HostApiTypeId> {
        match hostapi.to_ascii_lowercase().as_str() {
            "mme" => Ok(pa::HostApiTypeId::MME),
            "directsound" | "ds" => Ok(pa::HostApiTypeId::DirectSound),
            "wasapi" => Ok(pa::HostApiTypeId::WASAPI),
            "asio" => Ok(pa::HostApiTypeId::ASIO),
            other => Err(anyhow!("unknown hostapi: {other}")),
        }
    }

    fn hostapi_index(&self, hostapi: &str) -> Result<pa::HostApiIndex> {
        let tid = Self::hostapi_type_from_name(hostapi)?;
        self.pa
            .host_api_type_id_to_host_api_index(tid)
            .map_err(|e| anyhow!("hostapi not available: {hostapi} ({e:?})"))
    }

    fn devices_for_hostapi(&self, hostapi: &str) -> Result<Vec<(pa::DeviceIndex, pa::DeviceInfo)>> {
        let host_idx = self.hostapi_index(hostapi)?;
        let host_info = self
            .pa
            .host_api_info(host_idx)
            .ok_or_else(|| anyhow!("failed to query hostapi info: {hostapi}"))?;

        let mut out = Vec::new();
        for api_dev_idx in 0..(host_info.device_count as i32) {
            let dev_idx = self
                .pa
                .api_device_index_to_device_index(host_idx, api_dev_idx)
                .map_err(|e| anyhow!("device index conversion failed: {e:?}"))?;
            let info = self
                .pa
                .device_info(dev_idx)
                .map_err(|e| anyhow!("device info failed: {e:?}"))?;
            out.push((dev_idx, info));
        }
        Ok(out)
    }

    fn select_device(&self, hostapi: &str, direction: Direction, sel: &DeviceSelectorKind) -> Result<pa::DeviceIndex> {
        let host_idx = self.hostapi_index(hostapi)?;
        let host_info = self
            .pa
            .host_api_info(host_idx)
            .ok_or_else(|| anyhow!("failed to query hostapi info: {hostapi}"))?;

        let default_dev = match direction {
            Direction::Input => host_info.default_input_device,
            Direction::Output => host_info.default_output_device,
        };

        match sel {
            DeviceSelectorKind::Default => default_dev.ok_or_else(|| anyhow!("no default device for {hostapi}")),
            DeviceSelectorKind::NameContains(needle) => {
                for (idx, info) in self.devices_for_hostapi(hostapi)? {
                    let ok_dir = match direction {
                        Direction::Input => info.max_input_channels > 0,
                        Direction::Output => info.max_output_channels > 0,
                    };
                    if ok_dir && info.name.contains(needle) {
                        return Ok(idx);
                    }
                }
                Err(anyhow!("device not found: {needle}"))
            }
        }
    }
}

struct PaInputHandle {
    stream: pa::Stream<pa::NonBlocking, pa::Input<f32>>,
}

impl StreamHandle for PaInputHandle {
    fn start(&mut self) -> Result<()> {
        self.stream.start().map_err(|e| anyhow!("{e:?}"))?;
        Ok(())
    }

    fn stop(&mut self) -> Result<()> {
        self.stream.stop().map_err(|e| anyhow!("{e:?}"))?;
        Ok(())
    }
}

struct PaOutputHandle {
    stream: pa::Stream<pa::NonBlocking, pa::Output<f32>>,
}

impl StreamHandle for PaOutputHandle {
    fn start(&mut self) -> Result<()> {
        self.stream.start().map_err(|e| anyhow!("{e:?}"))?;
        Ok(())
    }

    fn stop(&mut self) -> Result<()> {
        self.stream.stop().map_err(|e| anyhow!("{e:?}"))?;
        Ok(())
    }
}

impl AudioBackend for PortAudioBackend {
    fn name(&self) -> &'static str {
        "portaudio"
    }

    fn list_hostapis(&self) -> Vec<String> {
        // Only return host APIs actually available in this PortAudio build/runtime.
        // (Some portaudio.dll builds omit ASIO support due to ASIO SDK licensing.)
        let candidates: [(&str, pa::HostApiTypeId); 4] = [
            ("MME", pa::HostApiTypeId::MME),
            ("DirectSound", pa::HostApiTypeId::DirectSound),
            ("WASAPI", pa::HostApiTypeId::WASAPI),
            ("ASIO", pa::HostApiTypeId::ASIO),
        ];
        let mut out = Vec::new();
        for (name, tid) in candidates {
            if self.pa.host_api_type_id_to_host_api_index(tid).is_ok() {
                out.push(name.to_string());
            }
        }
        out
    }

    fn list_devices(&self, hostapi: &str, direction: Direction) -> Result<Vec<DeviceInfo>> {
        let mut out = Vec::new();
        for (_idx, info) in self.devices_for_hostapi(hostapi)? {
            let ok_dir = match direction {
                Direction::Input => info.max_input_channels > 0,
                Direction::Output => info.max_output_channels > 0,
            };
            if !ok_dir {
                continue;
            }
            out.push(DeviceInfo {
                name: info.name.to_string(),
                max_input_channels: info.max_input_channels as u16,
                max_output_channels: info.max_output_channels as u16,
                default_sr: info.default_sample_rate as u32,
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
        let dev = self.select_device(&device.hostapi, Direction::Input, &device.selector)?;
        let info = self.pa.device_info(dev).map_err(|e| anyhow!("{e:?}"))?;
        let latency = info.default_low_input_latency;

        let params = pa::StreamParameters::<f32>::new(dev, cfg.channels as i32, true, latency);
        let settings = pa::InputStreamSettings::new(params, cfg.sr as f64, pa::FRAMES_PER_BUFFER_UNSPECIFIED);

        let stream = self
            .pa
            .open_non_blocking_stream(settings, move |args: pa::InputStreamCallbackArgs<f32>| {
                callback(args.buffer);
                pa::Continue
            })
            .map_err(|e| anyhow!("open input stream failed: {e:?}"))?;

        let handle = PaInputHandle { stream };
        Ok(Box::new(handle))
    }

    fn open_output(
        &self,
        cfg: StreamConfig,
        device: DeviceSelector,
        mut callback: Box<dyn FnMut(&mut [f32]) + Send>,
    ) -> Result<Box<dyn StreamHandle>> {
        let dev = self.select_device(&device.hostapi, Direction::Output, &device.selector)?;
        let info = self.pa.device_info(dev).map_err(|e| anyhow!("{e:?}"))?;
        let latency = info.default_low_output_latency;

        let params = pa::StreamParameters::<f32>::new(dev, cfg.channels as i32, true, latency);
        let settings = pa::OutputStreamSettings::new(params, cfg.sr as f64, pa::FRAMES_PER_BUFFER_UNSPECIFIED);

        let stream = self
            .pa
            .open_non_blocking_stream(settings, move |args: pa::OutputStreamCallbackArgs<f32>| {
                callback(args.buffer);
                pa::Continue
            })
            .map_err(|e| anyhow!("open output stream failed: {e:?}"))?;

        let handle = PaOutputHandle { stream };
        Ok(Box::new(handle))
    }
}

