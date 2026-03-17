use crate::backends::BackendKind;
use crate::engine::metrics::{Metrics, MetricsSnapshot};
use crate::engine::routes::{
    CaptureReadReply, Cmd, DeviceListReply, HostApiListReply, SessionParams, SessionStartReply,
};
use crate::engine::session::Session;
use anyhow::{anyhow, Result};
use serde_json::Value;
use std::collections::HashMap;
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

    pub fn session_stop_all(&self) -> Result<()> {
        self.dispatch(Cmd::SessionStopAll {}).map(|_| ())
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

struct SessionSlot {
    session: Session,
    metrics: Arc<Metrics>,
    status: EngineStatus,
}

struct EngineState {
    sessions: HashMap<String, SessionSlot>,
    next_id: u64,
}

struct Request {
    cmd: Cmd,
    resp_tx: mpsc::Sender<Result<Value>>,
}

const DEFAULT_SESSION_ID: &str = "";

impl EngineState {
    fn resolve_id(&mut self, requested: &str) -> String {
        if requested.is_empty() {
            DEFAULT_SESSION_ID.to_string()
        } else {
            requested.to_string()
        }
    }

    fn alloc_id(&mut self) -> String {
        self.next_id += 1;
        format!("s{}", self.next_id)
    }
}

fn engine_thread_main(rx: mpsc::Receiver<Request>) {
    let mut st = EngineState {
        sessions: HashMap::new(),
        next_id: 0,
    };

    while let Ok(req) = rx.recv() {
        let out = dispatch_in_thread(&mut st, req.cmd);
        let _ = req.resp_tx.send(out);
    }
}

fn dispatch_in_thread(st: &mut EngineState, cmd: Cmd) -> Result<Value> {
    cleanup_finished_sessions(st);
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

        Cmd::SessionStart { session_id, params, .. } => {
            session_start_in_thread(st, &session_id, params)
        }

        Cmd::SessionStop { session_id, .. } => {
            let sid = st.resolve_id(&session_id);
            session_stop_in_thread(st, &sid)?;
            Ok(serde_json::json!({"msg":"stopped","session_id":sid}))
        }

        Cmd::SessionStopAll { .. } => {
            session_stop_all_in_thread(st)?;
            Ok(serde_json::json!({"msg":"all stopped"}))
        }

        Cmd::Status { session_id, .. } => status_json_in_thread(st, &session_id),

        Cmd::CaptureRead { session_id, max_frames, .. } => {
            let sid = st.resolve_id(&session_id);
            if let Some(slot) = st.sessions.get_mut(&sid) {
                let reply = slot.session.capture_read(max_frames)?;
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

        Cmd::PlayWrite { session_id, pcm16_b64, .. } => {
            let sid = st.resolve_id(&session_id);
            let slot = st.sessions.get_mut(&sid)
                .ok_or_else(|| anyhow!("no active session (id={sid})"))?;
            let accepted_frames = slot.session.play_write_pcm16_base64(&pcm16_b64)?;
            Ok(serde_json::json!({"msg":"ok","accepted_frames":accepted_frames}))
        }

        Cmd::PlayFinish { session_id, .. } => {
            let sid = st.resolve_id(&session_id);
            let slot = st.sessions.get_mut(&sid)
                .ok_or_else(|| anyhow!("no active session (id={sid})"))?;
            slot.session.play_finish()?;
            Ok(serde_json::json!({"msg":"ok"}))
        }
    }
}

fn status_json_in_thread(st: &mut EngineState, session_id: &str) -> Result<Value> {
    cleanup_finished_sessions(st);

    if session_id.is_empty() && st.sessions.len() <= 1 {
        // Legacy single-session behavior: return the only session or idle.
        if let Some((sid, slot)) = st.sessions.iter().next() {
            let metrics: MetricsSnapshot = slot.metrics.snapshot();
            return Ok(serde_json::json!({
                "status": format!("{:?}", slot.status),
                "has_session": true,
                "session_id": sid,
                "metrics": metrics,
            }));
        }
        return Ok(serde_json::json!({
            "status": "Idle",
            "has_session": false,
            "session_id": "",
            "metrics": null,
        }));
    }

    let sid = if session_id.is_empty() {
        DEFAULT_SESSION_ID.to_string()
    } else {
        session_id.to_string()
    };

    if let Some(slot) = st.sessions.get(&sid) {
        let metrics: MetricsSnapshot = slot.metrics.snapshot();
        Ok(serde_json::json!({
            "status": format!("{:?}", slot.status),
            "has_session": true,
            "session_id": sid,
            "metrics": metrics,
        }))
    } else {
        Ok(serde_json::json!({
            "status": "Idle",
            "has_session": false,
            "session_id": sid,
            "metrics": null,
        }))
    }
}

fn session_stop_in_thread(st: &mut EngineState, sid: &str) -> Result<()> {
    if let Some(mut slot) = st.sessions.remove(sid) {
        slot.status = EngineStatus::Stopping;
        let r = slot.session.stop();
        slot.status = EngineStatus::Finished;
        r?;
    }
    Ok(())
}

fn session_stop_all_in_thread(st: &mut EngineState) -> Result<()> {
    let keys: Vec<String> = st.sessions.keys().cloned().collect();
    for sid in keys {
        session_stop_in_thread(st, &sid)?;
    }
    Ok(())
}

fn session_start_in_thread(st: &mut EngineState, requested_id: &str, params: SessionParams) -> Result<Value> {
    // Determine session ID: use the requested one, or auto-generate if empty.
    let sid = if requested_id.is_empty() {
        // Legacy behavior: if no session_id given, stop the default session (backward compat).
        session_stop_in_thread(st, DEFAULT_SESSION_ID)?;
        DEFAULT_SESSION_ID.to_string()
    } else {
        // Explicit ID: stop any existing session with the same ID, then reuse that ID.
        session_stop_in_thread(st, requested_id)?;
        requested_id.to_string()
    };

    let backend_kind = BackendKind::from_str(&params.backend)?;
    let backend = crate::backends::create_backend(backend_kind)?;

    let metrics = Arc::new(Metrics::new());
    let session = Session::start(backend, params.clone(), metrics.clone())?;

    st.sessions.insert(sid.clone(), SessionSlot {
        session,
        metrics,
        status: EngineStatus::Running,
    });

    Ok(serde_json::to_value(SessionStartReply {
        msg: "started".to_string(),
        session_id: sid,
    })?)
}

fn cleanup_finished_sessions(st: &mut EngineState) {
    let finished_keys: Vec<String> = st
        .sessions
        .iter()
        .filter(|(_, slot)| slot.session.is_finished())
        .map(|(k, _)| k.clone())
        .collect();
    for k in finished_keys {
        st.sessions.remove(&k);
    }
}
