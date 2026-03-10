use crate::backends::BackendKind;
use crate::engine::metrics::{Metrics, MetricsSnapshot};
use crate::engine::routes::{
    CaptureReadReply, Cmd, DeviceListReply, HostApiListReply, SessionParams, SessionStartReply,
};
use crate::engine::session::Session;
use anyhow::{anyhow, Result};
use serde_json::Value;
use std::sync::{mpsc, Arc};
use std::thread;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EngineStatus {
    Idle,
    Running,
    Stopping,
    Finished,
    Error,
}

#[derive(Clone)]
pub struct Engine {
    tx: mpsc::Sender<Request>,
}

impl Engine {
    pub fn new() -> Self {
        let (tx, rx) = mpsc::channel::<Request>();
        thread::spawn(move || engine_thread_main(rx));
        Self { tx }
    }

    pub fn session_stop(&self) -> Result<()> {
        self.dispatch(Cmd::SessionStop {}).map(|_| ())
    }

    pub fn dispatch_json_line(&self, line: &str) -> Result<Value> {
        let cmd: Cmd = serde_json::from_str(line).map_err(|e| anyhow!("bad json: {e}"))?;
        self.dispatch(cmd)
    }

    fn dispatch(&self, cmd: Cmd) -> Result<Value> {
        let (resp_tx, resp_rx) = mpsc::channel::<Result<Value>>();
        self.tx
            .send(Request { cmd, resp_tx })
            .map_err(|_| anyhow!("engine thread is not running"))?;
        resp_rx
            .recv()
            .map_err(|_| anyhow!("engine thread did not reply"))?
    }
}

struct EngineState {
    status: EngineStatus,
    last_error: Option<String>,
    session: Option<Session>,
    metrics: Arc<Metrics>,
}

struct Request {
    cmd: Cmd,
    resp_tx: mpsc::Sender<Result<Value>>,
}

fn engine_thread_main(rx: mpsc::Receiver<Request>) {
    let mut st = EngineState {
        status: EngineStatus::Idle,
        last_error: None,
        session: None,
        metrics: Arc::new(Metrics::new()),
    };

    while let Ok(req) = rx.recv() {
        let out = dispatch_in_thread(&mut st, req.cmd);
        let _ = req.resp_tx.send(out);
    }
}

fn dispatch_in_thread(st: &mut EngineState, cmd: Cmd) -> Result<Value> {
    cleanup_finished_session(st);
    match cmd {
        Cmd::ListBackends { .. } => Ok(serde_json::to_value(serde_json::json!({
            "backends": ["cpal", "portaudio"]
        }))?),

        Cmd::ListHostApis { backend, .. } => {
            let b = BackendKind::from_str(&backend)?;
            let backend = crate::backends::create_backend(b)?;
            let hostapis = backend.list_hostapis();
            Ok(serde_json::to_value(HostApiListReply { hostapis })?)
        }

        Cmd::ListDevices {
            backend,
            hostapi,
            direction,
            ..
        } => {
            let b = BackendKind::from_str(&backend)?;
            let backend = crate::backends::create_backend(b)?;
            let devs = backend.list_devices(&hostapi, direction)?;
            Ok(serde_json::to_value(DeviceListReply { devices: devs })?)
        }

        Cmd::SessionStart { params, .. } => session_start_in_thread(st, params),

        Cmd::SessionStop { .. } => {
            session_stop_in_thread(st)?;
            Ok(serde_json::json!({"msg":"stopped"}))
        }

        Cmd::Status { .. } => status_json_in_thread(st),

        Cmd::CaptureRead { max_frames, .. } => {
            if let Some(s) = st.session.as_mut() {
                let reply = s.capture_read(max_frames)?;
                Ok(serde_json::to_value(reply)?)
            } else {
                Ok(serde_json::to_value(CaptureReadReply {
                    pcm16_b64: String::new(),
                    frames: 0,
                    channels: 0,
                    sr: 0,
                    eof: true,
                })?)
            }
        }

        Cmd::PlayWrite { pcm16_b64, .. } => {
            let s = st.session.as_mut().ok_or_else(|| anyhow!("no active session"))?;
            let accepted_frames = s.play_write_pcm16_base64(&pcm16_b64)?;
            Ok(serde_json::json!({"msg":"ok","accepted_frames":accepted_frames}))
        }

        Cmd::PlayFinish { .. } => {
            let s = st.session.as_mut().ok_or_else(|| anyhow!("no active session"))?;
            s.play_finish()?;
            Ok(serde_json::json!({"msg":"ok"}))
        }
    }
}

fn status_json_in_thread(st: &mut EngineState) -> Result<Value> {
    cleanup_finished_session(st);
    let metrics: MetricsSnapshot = st.metrics.snapshot();
    Ok(serde_json::json!({
        "status": format!("{:?}", st.status),
        "last_error": st.last_error,
        "metrics": metrics,
        "has_session": st.session.is_some(),
    }))
}

fn session_stop_in_thread(st: &mut EngineState) -> Result<()> {
    if let Some(mut s) = st.session.take() {
        st.status = EngineStatus::Stopping;
        let r = s.stop();
        st.status = EngineStatus::Finished;
        r?;
    }
    Ok(())
}

fn session_start_in_thread(st: &mut EngineState, params: SessionParams) -> Result<Value> {
    session_stop_in_thread(st)?;

    let backend_kind = BackendKind::from_str(&params.backend)?;
    let backend = crate::backends::create_backend(backend_kind)?;

    let metrics = Arc::new(Metrics::new());
    let session = Session::start(backend, params.clone(), metrics.clone())?;

    st.metrics = metrics;
    st.status = EngineStatus::Running;
    st.session = Some(session);

    Ok(serde_json::to_value(SessionStartReply {
        msg: "started".to_string(),
    })?)
}

fn cleanup_finished_session(st: &mut EngineState) {
    let finished = st.session.as_ref().is_some_and(|s| s.is_finished());
    if finished {
        st.session.take();
        st.status = EngineStatus::Finished;
    }
}

