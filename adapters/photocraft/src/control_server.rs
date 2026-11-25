//! Localhost JSON-lines control server (one request per line, one reply per line).
//! Loopback only. This is the transport the MCP server will wrap.
//!
//! `--control` starts it for the whole run ([`start`]); Preferences › AI Integrations starts and
//! stops it while the app runs ([`listen`], see `agent_link`).

use std::io::{BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{Receiver, Sender, channel};
use std::time::{Duration, Instant};

use photocraft_automation::budgets::write_reply;
use photocraft_automation::security::{
    AUTH_DEADLINE, AuthDeadlineStream, AuthStep, ConnectionLimiter, LineRead, MAX_CONNECTIONS, MAX_REQUEST_BYTES, ServerHandshake, configure_stream,
    read_bounded_line, repeat_auth_reply,
};
use photocraft_ui_egui::ControlRequest;
use photocraft_ui_egui::control::REPLY_DEADLINE;
use serde_json::{Value, json};

/// Start serving a listener already bound during startup, before a window is opened. It serves
/// for the whole run.
pub fn start(listener: TcpListener, token: String, ctx: egui::Context) -> Receiver<ControlRequest> {
    let (tx, rx) = channel::<ControlRequest>();
    let port = listener.local_addr().map_or(0, |a| a.port());
    eprintln!("photocraft: control server listening on {}", listening_addr(&listener, port));
    std::thread::spawn(move || accept(listener, token, tx, ctx, None));
    rx
}

/// A control server started by [`listen`]. Dropping it stops accepting connections; connections
/// already open end at their next request, once the app drops the request channel.
pub struct Listening {
    /// The bound port (the one the system picked for port 0).
    pub port: u16,
    stop: Arc<AtomicBool>,
    accept: Option<std::thread::JoinHandle<()>>,
}

impl Drop for Listening {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        // Wake the accept loop so it sees the flag and closes the listener, and wait for that, so
        // the port is free when this returns (a restart on the same port binds it again at once).
        // Without a wake-up the loop would block in `accept`: then don't wait.
        let woke = TcpStream::connect_timeout(&std::net::SocketAddr::from(([127, 0, 0, 1], self.port)), Duration::from_millis(500)).is_ok();
        if woke && let Some(accept) = self.accept.take() {
            let _ = accept.join();
        }
    }
}

/// Serve the control protocol on `127.0.0.1:port` (0 = any free port) until the returned
/// [`Listening`] is dropped.
pub fn listen(port: u16, token: String, ctx: egui::Context) -> std::io::Result<(Receiver<ControlRequest>, Listening)> {
    let (tx, rx) = channel::<ControlRequest>();
    let listener = TcpListener::bind(("127.0.0.1", port))?;
    let port = listener.local_addr()?.port();
    let stop = Arc::new(AtomicBool::new(false));
    let stopped = stop.clone();
    let accept = std::thread::Builder::new().name("control server".into()).spawn(move || accept(listener, token, tx, ctx, Some(stopped)))?;
    Ok((rx, Listening { port, stop, accept: Some(accept) }))
}

/// Accept connections on `listener` and serve each on its own thread, until `stop` is set (never,
/// without one).
fn accept(listener: TcpListener, token: String, tx: Sender<ControlRequest>, ctx: egui::Context, stop: Option<Arc<AtomicBool>>) {
    let limiter = ConnectionLimiter::new(MAX_CONNECTIONS);
    let token = Arc::new(token);
    for mut stream in listener.incoming().flatten() {
        if stop.as_ref().is_some_and(|s| s.load(Ordering::Acquire)) {
            break;
        }
        let Some(permit) = limiter.try_acquire() else {
            let _ = configure_stream(&stream);
            let _ = writeln!(stream, "{}", json!({"id": null, "ok": false, "error": "connection limit reached"}));
            continue;
        };
        let tx = tx.clone();
        let ctx = ctx.clone();
        let token = Arc::clone(&token);
        std::thread::spawn(move || {
            let _permit = permit;
            serve(stream, &token, tx, ctx);
        });
    }
}

/// The address `listener` is bound to: with `--control 0` the OS picks the port, and a client
/// needs that one, not the requested 0 (#6427). Falls back to the requested port if the OS
/// can't report it.
fn listening_addr(listener: &TcpListener, requested_port: u16) -> String {
    match listener.local_addr() {
        Ok(addr) => addr.to_string(),
        Err(_) => format!("127.0.0.1:{requested_port}"),
    }
}

fn serve(stream: TcpStream, token: &str, tx: Sender<ControlRequest>, ctx: egui::Context) {
    serve_with_auth_deadline(stream, token, tx, ctx, AUTH_DEADLINE);
}

/// [`serve`] for a connection that must complete `auth` within `auth_deadline` of connecting,
/// however much it sends meanwhile (#4965); after that only the idle timeout applies.
fn serve_with_auth_deadline(stream: TcpStream, token: &str, tx: Sender<ControlRequest>, ctx: egui::Context, auth_deadline: Duration) {
    if configure_stream(&stream).is_err() {
        return;
    }
    let Ok(read) = stream.try_clone() else { return };
    let mut reader = BufReader::new(AuthDeadlineStream::new(read, auth_deadline));
    let mut out = stream;
    let mut line = String::new();
    let mut handshake = ServerHandshake::new();
    let mut authenticated = false;
    loop {
        match read_bounded_line(&mut reader, &mut line) {
            Ok(LineRead::Eof) | Err(_) => break,
            Ok(LineRead::TooLong) => {
                let reply = json!({
                    "id": null,
                    "ok": false,
                    "error": format!("request exceeds {MAX_REQUEST_BYTES} bytes"),
                });
                let _ = write_reply(&mut out, &reply);
                let _ = out.flush();
                break;
            }
            Ok(LineRead::Line) if line.trim().is_empty() => continue,
            Ok(LineRead::Line) => {}
        }
        if !authenticated {
            let (reply, step) = handshake.reply(&line, token);
            authenticated = step == AuthStep::Authenticated;
            if authenticated && reader.get_mut().authenticated().is_err() {
                break;
            }
            if write_reply(&mut out, &reply).is_err() || out.flush().is_err() || step == AuthStep::Rejected {
                break;
            }
            continue;
        }
        if let Some(reply) = repeat_auth_reply(&line) {
            if write_reply(&mut out, &reply).is_err() || out.flush().is_err() {
                break;
            }
            continue;
        }
        let reply = match serde_json::from_str::<Value>(&line) {
            Ok(msg) => {
                let id = msg.get("id").cloned().unwrap_or(Value::Null);
                let method = msg.get("method").and_then(Value::as_str).unwrap_or("").to_string();
                let params = msg.get("params").cloned().unwrap_or(json!({}));
                let deadline = Instant::now() + REPLY_DEADLINE;
                let (mut req, rrx) = ControlRequest::new(method, params);
                req.deadline = Some(deadline);
                let dispatch = req.dispatch.clone();
                if tx.send(req).is_err() {
                    break;
                }
                ctx.request_repaint();
                let mut r =
                    rrx.recv_timeout(deadline.saturating_duration_since(Instant::now())).unwrap_or_else(|_| json!({"ok": false, "error": dispatch.give_up()}));
                if let Some(o) = r.as_object_mut() {
                    o.insert("id".into(), id);
                }
                r
            }
            Err(e) => json!({"id": null, "ok": false, "error": format!("bad JSON: {e}")}),
        };
        if write_reply(&mut out, &reply).is_err() {
            break;
        }
        if out.flush().is_err() {
            break;
        }
    }
}
