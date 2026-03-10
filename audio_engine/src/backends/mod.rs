pub mod cpal_backend;
#[cfg(feature = "portaudio_backend")]
pub mod pa_backend;

#[cfg(not(feature = "portaudio_backend"))]
pub mod pa_backend {
    use super::*;
    use anyhow::{anyhow, Result};

    pub struct PortAudioBackend;

    impl PortAudioBackend {
        pub fn new() -> Result<Self> {
            // Keep list_hostapis working even when the backend is disabled.
            Ok(Self)
        }
    }

    impl AudioBackend for PortAudioBackend {
        fn name(&self) -> &'static str {
            "portaudio"
        }

        fn list_hostapis(&self) -> Vec<String> {
            vec![
                "MME".to_string(),
                "DirectSound".to_string(),
                "WASAPI".to_string(),
                "ASIO".to_string(),
            ]
        }

        fn list_devices(&self, _hostapi: &str, _direction: Direction) -> Result<Vec<DeviceInfo>> {
            Err(anyhow!(
                "PortAudio backend is disabled. Rebuild with --features portaudio_backend and provide portaudio.lib/portaudio.dll."
            ))
        }

        fn open_input(
            &self,
            _cfg: StreamConfig,
            _device: DeviceSelector,
            _callback: Box<dyn FnMut(&[f32]) + Send>,
        ) -> Result<Box<dyn StreamHandle>> {
            Err(anyhow!(
                "PortAudio backend is disabled. Rebuild with --features portaudio_backend and provide portaudio.lib/portaudio.dll."
            ))
        }

        fn open_output(
            &self,
            _cfg: StreamConfig,
            _device: DeviceSelector,
            _callback: Box<dyn FnMut(&mut [f32]) + Send>,
        ) -> Result<Box<dyn StreamHandle>> {
            Err(anyhow!(
                "PortAudio backend is disabled. Rebuild with --features portaudio_backend and provide portaudio.lib/portaudio.dll."
            ))
        }
    }
}

use anyhow::{anyhow, Result};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Direction {
    Input,
    Output,
}

#[derive(Debug, Clone, Serialize)]
pub struct DeviceInfo {
    pub name: String,
    pub max_input_channels: u16,
    pub max_output_channels: u16,
    pub default_sr: u32,
}

#[derive(Debug, Clone, Copy)]
pub struct StreamConfig {
    pub sr: u32,
    pub channels: u16,
}

#[derive(Debug, Clone)]
pub struct DeviceSelector {
    pub hostapi: String,
    pub selector: DeviceSelectorKind,
}

#[derive(Debug, Clone)]
pub enum DeviceSelectorKind {
    Default,
    NameContains(String),
}

pub trait StreamHandle: Send {
    fn start(&mut self) -> Result<()>;
    fn stop(&mut self) -> Result<()>;
}

pub trait AudioBackend: Send {
    fn name(&self) -> &'static str;

    fn list_hostapis(&self) -> Vec<String>;

    fn list_devices(&self, hostapi: &str, direction: Direction) -> Result<Vec<DeviceInfo>>;

    fn open_input(
        &self,
        cfg: StreamConfig,
        device: DeviceSelector,
        callback: Box<dyn FnMut(&[f32]) + Send>,
    ) -> Result<Box<dyn StreamHandle>>;

    fn open_output(
        &self,
        cfg: StreamConfig,
        device: DeviceSelector,
        callback: Box<dyn FnMut(&mut [f32]) + Send>,
    ) -> Result<Box<dyn StreamHandle>>;
}

#[derive(Debug, Clone, Copy)]
pub enum BackendKind {
    Cpal,
    PortAudio,
}

impl BackendKind {
    pub fn from_str(s: &str) -> Result<Self> {
        match s.to_ascii_lowercase().as_str() {
            "cpal" => Ok(Self::Cpal),
            "portaudio" | "pa" => Ok(Self::PortAudio),
            other => Err(anyhow!("unknown backend: {other}")),
        }
    }
}

pub fn create_backend(kind: BackendKind) -> Result<Box<dyn AudioBackend>> {
    match kind {
        BackendKind::Cpal => Ok(Box::new(cpal_backend::CpalBackend::new())),
        BackendKind::PortAudio => Ok(Box::new(pa_backend::PortAudioBackend::new()?)),
    }
}

