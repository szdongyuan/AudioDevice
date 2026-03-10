use crate::engine::metrics::Metrics;
use anyhow::Result;
use rtrb::RingBuffer;
use std::sync::Arc;
use std::sync::atomic::Ordering;

#[derive(Clone)]
pub struct AudioRing {
    sr: u32,
    ch: u16,
    prod: Arc<std::sync::Mutex<rtrb::Producer<f32>>>,
    cons: Arc<std::sync::Mutex<rtrb::Consumer<f32>>>,
}

impl AudioRing {
    pub fn new(sr: u32, ch: u16, rb_seconds: u32) -> Result<Self> {
        let frames = (sr as usize) * (rb_seconds.max(1) as usize);
        let samples = frames * (ch.max(1) as usize);
        let (prod, cons) = RingBuffer::<f32>::new(samples);
        Ok(Self {
            sr,
            ch,
            prod: Arc::new(std::sync::Mutex::new(prod)),
            cons: Arc::new(std::sync::Mutex::new(cons)),
        })
    }

    pub fn sr(&self) -> u32 {
        self.sr
    }

    pub fn channels(&self) -> u16 {
        self.ch
    }

    pub fn push_samples(&mut self, samples: &[f32]) -> Result<()> {
        let mut p = self.prod.lock().unwrap();
        for &x in samples {
            let _ = p.push(x);
        }
        Ok(())
    }

    pub fn pop_samples(&mut self, out: &mut [f32]) -> Result<usize> {
        let mut c = self.cons.lock().unwrap();
        let mut n = 0usize;
        for dst in out.iter_mut() {
            match c.pop() {
                Ok(v) => {
                    *dst = v;
                    n += 1;
                }
                Err(_) => break,
            }
        }
        Ok(n)
    }

    pub fn clone_for_callback(&self) -> Self {
        Self {
            sr: self.sr,
            ch: self.ch,
            prod: self.prod.clone(),
            cons: self.cons.clone(),
        }
    }

    pub fn push_samples_nonblocking(&mut self, samples: &[f32], metrics: &Metrics) -> Result<()> {
        let mut p = self.prod.lock().unwrap();
        for &x in samples {
            if p.push(x).is_err() {
                metrics.overruns.fetch_add(1, Ordering::Relaxed);
                break;
            }
        }
        Ok(())
    }

    pub fn pop_into_slice_nonblocking(&mut self, out: &mut [f32], metrics: &Metrics) -> Result<()> {
        let mut c = self.cons.lock().unwrap();
        for dst in out.iter_mut() {
            match c.pop() {
                Ok(v) => *dst = v,
                Err(_) => {
                    *dst = 0.0;
                    metrics.underruns.fetch_add(1, Ordering::Relaxed);
                }
            }
        }
        Ok(())
    }

    pub fn available_samples(&self) -> usize {
        let c = self.cons.lock().unwrap();
        c.slots()
    }

    pub fn is_empty(&self) -> bool {
        let c = self.cons.lock().unwrap();
        c.is_empty()
    }

    pub fn push_samples_partial_nonblocking(
        &mut self,
        samples: &[f32],
        align_to: usize,
    ) -> Result<usize> {
        let mut p = self.prod.lock().unwrap();
        let slots = p.slots();
        if slots == 0 {
            return Ok(0);
        }

        let mut n = slots.min(samples.len());
        if align_to > 1 {
            n -= n % align_to;
        }
        if n == 0 {
            return Ok(0);
        }

        for &x in &samples[..n] {
            if p.push(x).is_err() {
                break;
            }
        }
        Ok(n)
    }
}

