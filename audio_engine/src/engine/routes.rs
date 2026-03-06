use crate::backends::Direction;
use serde::{Deserialize, Serialize};

#[derive(Debug, Deserialize)]
#[serde(tag = "cmd")]
pub enum Cmd {
    #[serde(rename = "list_backends")]
    ListBackends {},

    #[serde(rename = "list_hostapis")]
    ListHostApis { backend: String },

    #[serde(rename = "list_devices")]
    ListDevices {
        backend: String,
        hostapi: String,
        direction: Direction,
    },

    #[serde(rename = "session_start")]
    SessionStart { #[serde(flatten)] params: SessionParams },

    #[serde(rename = "session_stop")]
    SessionStop {},

    #[serde(rename = "status")]
    Status {},

    #[serde(rename = "capture_read")]
    CaptureRead { max_frames: usize },

    #[serde(rename = "play_write")]
    PlayWrite { pcm16_b64: String },

    #[serde(rename = "play_finish")]
    PlayFinish {},
}

#[derive(Debug, Clone, Deserialize)]
pub struct SessionParams {
    pub backend: String,
    pub hostapi: String,
    pub mode: String,

    pub sr: u32,
    pub in_ch: u16,
    pub out_ch: u16,

    #[serde(default)]
    pub device_in: String,
    #[serde(default)]
    pub device_out: String,

    #[serde(default)]
    pub duration_s: f64,
    #[serde(default)]
    pub rotate_s: f64,

    #[serde(default)]
    pub path: String,

    #[serde(default)]
    pub play_path: String,

    #[serde(default)]
    pub return_audio: bool,

    #[serde(default)]
    pub rb_seconds: u32,
}

#[derive(Debug, Serialize)]
pub struct HostApiListReply {
    pub hostapis: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct DeviceListReply<T> {
    pub devices: T,
}

#[derive(Debug, Serialize)]
pub struct SessionStartReply {
    pub msg: String,
}

#[derive(Debug, Serialize)]
pub struct CaptureReadReply {
    pub pcm16_b64: String,
    pub frames: usize,
    pub channels: u16,
    pub sr: u32,
    pub eof: bool,
}
