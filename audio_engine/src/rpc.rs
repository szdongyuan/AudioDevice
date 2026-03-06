use crate::engine::state::Engine;
use anyhow::Result;
use serde::Serialize;
use serde_json::json;
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::Arc;
use std::thread;

#[derive(Serialize)]
struct Reply<T: Serialize> {
    ok: bool,
    data: T,
    err: Option<String>,
}

fn reply_ok<T: Serialize>(data: T) -> String {
    serde_json::to_string(&Reply {
        ok: true,
        data,
        err: None,
    })
    .unwrap()
        + "\n"
}

fn reply_err(msg: impl Into<String>) -> String {
    serde_json::to_string(&Reply::<serde_json::Value> {
        ok: false,
        data: json!({}),
        err: Some(msg.into()),
    })
    .unwrap()
        + "\n"
}

pub fn serve(addr: &str, engine: Arc<Engine>) -> Result<()> {
    let listener = TcpListener::bind(addr)?;
    for conn in listener.incoming() {
        match conn {
            Ok(stream) => {
                let engine = engine.clone();
                thread::spawn(move || {
                    if let Err(e) = handle_client(stream, engine) {
                        eprintln!("rpc client error: {e}");
                    }
                });
            }
            Err(e) => eprintln!("rpc accept error: {e}"),
        }
    }
    Ok(())
}

fn handle_client(stream: TcpStream, engine: Arc<Engine>) -> Result<()> {
    let reader = BufReader::new(stream.try_clone()?);
    let mut writer = BufWriter::new(stream);

    for line in reader.lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }

        let resp = match engine.dispatch_json_line(&line) {
            Ok(data) => reply_ok(data),
            Err(e) => reply_err(e.to_string()),
        };
        writer.write_all(resp.as_bytes())?;
        writer.flush()?;
    }

    Ok(())
}

