use anyhow::{anyhow, Result};
use base64::Engine;

pub fn i16_to_f32(x: i16) -> f32 {
    (x as f32) / (i16::MAX as f32)
}

pub fn f32_to_i16(x: f32) -> i16 {
    let y = x.clamp(-1.0, 1.0);
    (y * (i16::MAX as f32)) as i16
}

pub fn i32_to_f32(x: i32) -> f32 {
    (x as f32) / (i32::MAX as f32)
}

pub fn f32_to_i32(x: f32) -> i32 {
    let y = x.clamp(-1.0, 1.0);
    (y * (i32::MAX as f32)) as i32
}

pub fn u16_to_f32(x: u16) -> f32 {
    let centered = (x as i32) - 32768;
    (centered as f32) / (i16::MAX as f32)
}

pub fn u8_to_f32(x: u8) -> f32 {
    // Unsigned 8-bit PCM: 128 is (approximately) zero.
    let centered = (x as i32) - 128;
    (centered as f32) / 127.0
}

pub fn f32_to_u8(x: f32) -> u8 {
    let y = x.clamp(-1.0, 1.0);
    let v = (y * 127.0).round() as i32 + 128;
    v.clamp(0, 255) as u8
}

pub fn f32_to_pcm16_bytes_interleaved(samples: &[f32]) -> Vec<u8> {
    let mut out = Vec::with_capacity(samples.len() * 2);
    for &x in samples {
        out.extend_from_slice(&f32_to_i16(x).to_le_bytes());
    }
    out
}

pub fn pcm16_bytes_to_f32_interleaved(bytes: &[u8]) -> Result<Vec<f32>> {
    if bytes.len() % 2 != 0 {
        return Err(anyhow!("pcm16 bytes length must be even"));
    }
    let mut out = Vec::with_capacity(bytes.len() / 2);
    for chunk in bytes.chunks_exact(2) {
        let v = i16::from_le_bytes([chunk[0], chunk[1]]);
        out.push(i16_to_f32(v));
    }
    Ok(out)
}

pub fn base64_encode(bytes: &[u8]) -> String {
    base64::engine::general_purpose::STANDARD.encode(bytes)
}

pub fn base64_decode(s: &str) -> Result<Vec<u8>> {
    base64::engine::general_purpose::STANDARD
        .decode(s.as_bytes())
        .map_err(|e| anyhow!("base64 decode failed: {e}"))
}

