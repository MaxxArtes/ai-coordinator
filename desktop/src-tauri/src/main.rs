// AI Coordinator Desktop — Native Host (Tauri 2 / Rust)
//
// Sole authority of execution and governance remains with the Core Python runtime.
// The Rust host manages window lifecycle, native PTY bridging, and process tree telemetries.

#![windows_subsystem = "windows"]

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashSet;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant};
use sysinfo::{Pid, ProcessRefreshKind, System};
use tauri::{Emitter, State};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

// Title bar color of the "main" window: #071017, measured on the title bar of the operator's
// reference screenshot (the logo blue #001c44 is the alternative left unused).
// COLORREF layout is 0x00BBGGRR, so #071017 becomes 0x00171007.
#[cfg(windows)]
const MAIN_WINDOW_CAPTION_COLOR: u32 = 0x00171007;

// DWMWA_CAPTION_COLOR from dwmapi.h. Honoured from Windows 11 build 22000 on; Windows 10 answers
// E_INVALIDARG, which is ignored (the window keeps the default dark caption from "theme": "Dark").
#[cfg(windows)]
const DWMWA_CAPTION_COLOR: u32 = 35;

#[cfg(windows)]
#[allow(non_snake_case)]
#[link(name = "dwmapi")]
extern "system" {
    // The HWND goes as a pointer-sized integer: same ABI as the `void*` handle, and the cast below
    // compiles whether tauri's `windows::Win32::Foundation::HWND` wraps `*mut c_void` (current
    // `windows` releases) or `isize` (older ones).
    fn DwmSetWindowAttribute(
        hwnd: isize,
        dw_attribute: u32,
        pv_attribute: *const std::ffi::c_void,
        cb_attribute: u32,
    ) -> i32;
}

/// Paints the title bar of the "main" window with MAIN_WINDOW_CAPTION_COLOR.
/// Best effort: a missing window, a missing handle or a failing HRESULT keeps the default caption
/// and never panics nor aborts startup.
#[cfg(windows)]
fn apply_main_window_caption_color<R: tauri::Runtime>(app: &tauri::App<R>) {
    use tauri::Manager;

    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    let Ok(hwnd) = window.hwnd() else {
        return;
    };
    let color: u32 = MAIN_WINDOW_CAPTION_COLOR;
    // SAFETY: `hwnd` is the live handle of a window owned by this process (created before the
    // setup hook runs); `pv_attribute` points to a COLORREF on the stack that outlives the call and
    // `cb_attribute` is exactly its size. The call does not retain the pointer.
    let _hresult = unsafe {
        DwmSetWindowAttribute(
            hwnd.0 as isize,
            DWMWA_CAPTION_COLOR,
            &color as *const u32 as *const std::ffi::c_void,
            std::mem::size_of::<u32>() as u32,
        )
    };
}

#[derive(Serialize, Deserialize, Clone)]
pub struct RuntimeStatus {
    pub core_version: String,
    pub python_version: Option<String>,
    pub app_version: String,
    pub license: String,
    pub host_os: String,
    pub host_arch: String,
    pub is_isolated: bool,
    pub python_path: String,
}

#[derive(Serialize, Deserialize, Clone)]
pub struct ProcessTreeMetrics {
    pub host_pid: u32,
    pub host_ram_bytes: u64,
    pub total_tree_ram_bytes: u64,
    pub cpu_usage_percent: f32,
}

// ---- Core IPC transport (Marco 4D.3, Etapa 8: L1/L2) ----
// Every command that talks to the Core runs on the blocking pool (L1), so the window never waits for the Core.
// One NDJSON stream serves all of them: callers take turns in arrival order (FifoLock), a writer thread owns the
// Core's stdin, a reader thread owns its stdout, and each call waits a bounded time, counted from its start (the
// wait for the turn included), for the response that carries its own id (L2). Params and response content are
// never logged.

/// Error code of a transport failure between this host and the Core (no answer in time, stream closed).
const IPC_TRANSPORT_ERROR_CODE: i64 = -32007;

/// Frame limit of the Core (MAX_PAYLOAD_BYTES in desktop/ipc/protocol.py): UTF-8 bytes of one request line.
/// A larger request is refused here, because the Core answers it with a null id, which the bridge discards.
const IPC_MAX_REQUEST_BYTES: usize = 1024 * 1024;

/// The Core process is not running: it never started (no runtime found) or it was stopped for an update.
const IPC_CORE_NOT_RUNNING: &str = "O Core não está em execução (runtime não iniciado ou já encerrado).";

/// A blocking task that did not finish (panic or runtime shutting down). The join error itself is never shown:
/// a panic message could quote request data.
const IPC_TASK_FAILED_MESSAGE: &str = "Falha interna ao executar a chamada ao Core. Tente novamente.";

/// How long one exchange waits for its response (L2): 120 s for the methods that start terminal processes.
fn ipc_timeout_for(method: &str) -> Duration {
    match method {
        "terminal.create_tab" | "terminal.create_session" | "terminal.restore_sessions" => Duration::from_secs(120),
        _ => Duration::from_secs(30),
    }
}

/// Deadline of a call that starts now (L2): it covers the wait for the turn on the Core stream and the wait for
/// the response, so no call waits longer than ipc_timeout_for(method) in all.
fn ipc_deadline_for(method: &str) -> Instant {
    Instant::now() + ipc_timeout_for(method)
}

/// Transport failure of one exchange. It never carries params nor response content.
enum IpcFailure {
    /// No response with the request's id within the wait (in seconds).
    Timeout(u64),
    /// The Core closed the stream: its process exited.
    Disconnected,
    /// The request exceeds the Core's frame limit; nothing was sent.
    PayloadTooLarge,
    /// Nothing to talk to, or the request could not be encoded (fixed text).
    Unavailable(&'static str),
}

impl IpcFailure {
    fn message(&self) -> String {
        match self {
            IpcFailure::Timeout(secs) => format!(
                "O Core não respondeu em {} s. A operação pode ter sido concluída: confira antes de repetir.",
                secs
            ),
            IpcFailure::Disconnected => "O Core encerrou a comunicação. Reinicie o aplicativo.".to_string(),
            IpcFailure::PayloadTooLarge => {
                "A requisição excede o limite de 1 MiB aceito pelo Core e não foi enviada.".to_string()
            }
            IpcFailure::Unavailable(text) => (*text).to_string(),
        }
    }

    /// The {code, message, data} object of call_structured.
    fn to_structured(&self) -> Value {
        match self {
            IpcFailure::Timeout(_) => json!({
                "code": IPC_TRANSPORT_ERROR_CODE,
                "message": self.message(),
                "data": { "field": "ipc", "reason": "timeout" }
            }),
            IpcFailure::Disconnected => json!({
                "code": IPC_TRANSPORT_ERROR_CODE,
                "message": self.message(),
                "data": { "field": "ipc", "reason": "disconnected" }
            }),
            // Same code and data the Core uses for an oversized frame (PAYLOAD_TOO_LARGE).
            IpcFailure::PayloadTooLarge => json!({
                "code": -32001,
                "message": self.message(),
                "data": { "field": "request", "reason": "payload_too_large" }
            }),
            IpcFailure::Unavailable(_) => json!({ "code": -32603, "message": self.message() }),
        }
    }
}

/// Command error of a blocking task that did not finish (L1), as text.
fn ipc_task_failed() -> String {
    IPC_TASK_FAILED_MESSAGE.to_string()
}

/// Command error of a blocking task that did not finish (L1), as a structured {code, message, data} object.
fn ipc_task_failed_structured() -> Value {
    json!({
        "code": -32603,
        "message": IPC_TASK_FAILED_MESSAGE,
        "data": { "field": "ipc", "reason": "internal_error" }
    })
}

/// Locks a mutex whose content stays consistent if another holder panicked (a channel end, queue counters):
/// recovering keeps one panic from disabling every later IPC call.
fn lock_unpoisoned<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// First come, first served lock for the Core stream (std's Mutex wakes waiters in no defined order). Commands
/// run on the blocking pool (L1), so callers queued behind a slow call (a terminal starting, a Core that does
/// not answer) are served in the order their blocking tasks asked for the turn. That is not necessarily the order
/// in which the interface invoked them (the async dispatch can reorder two invokes of the same tick): the typing
/// order is kept by the interface, which has at most one write and one resize in flight per session
/// (desktop/src/components/terminalIo.ts). A panic while holding the turn passes it on (the guard is dropped on unwind).
/// IPC calls wait with lock_until: the wait for the turn counts in their deadline (L2), so a Core that hangs
/// answers every queued call with a timeout within its own deadline instead of one deadline per call ahead.
struct FifoLock {
    state: Mutex<FifoState>,
    turn_changed: Condvar,
}

struct FifoState {
    next_ticket: u64,
    now_serving: u64,
    // Tickets whose callers stopped waiting at their deadline (lock_until); the turn skips them.
    abandoned: HashSet<u64>,
}

impl FifoState {
    /// Passes the turn to the next ticket still waiting: abandoned tickets are skipped (and forgotten).
    fn advance(&mut self) {
        self.now_serving = self.now_serving.wrapping_add(1);
        while self.abandoned.remove(&self.now_serving) {
            self.now_serving = self.now_serving.wrapping_add(1);
        }
    }
}

struct FifoGuard<'a> {
    lock: &'a FifoLock,
}

impl FifoLock {
    fn new() -> Self {
        FifoLock {
            state: Mutex::new(FifoState {
                next_ticket: 0,
                now_serving: 0,
                abandoned: HashSet::new(),
            }),
            turn_changed: Condvar::new(),
        }
    }

    /// Waits for the turn without a limit. Only for the update freeze: every holder ahead of it gives the turn back
    /// within its own deadline (lock_until), so this wait is bounded too.
    fn lock(&self) -> FifoGuard<'_> {
        let mut state = lock_unpoisoned(&self.state);
        let ticket = state.next_ticket;
        state.next_ticket = state.next_ticket.wrapping_add(1);
        while state.now_serving != ticket {
            state = self
                .turn_changed
                .wait(state)
                .unwrap_or_else(|poisoned| poisoned.into_inner());
        }
        FifoGuard { lock: self }
    }

    /// Waits for the turn until `deadline` (L2: the deadline of an IPC call starts before its wait for the turn).
    /// None: the deadline passed first; the ticket is abandoned and the turn skips it, so nothing waits for this
    /// caller. A turn that comes together with the deadline is kept (Some).
    fn lock_until(&self, deadline: Instant) -> Option<FifoGuard<'_>> {
        let mut state = lock_unpoisoned(&self.state);
        let ticket = state.next_ticket;
        state.next_ticket = state.next_ticket.wrapping_add(1);
        loop {
            if state.now_serving == ticket {
                return Some(FifoGuard { lock: self });
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                // Not the turn (checked above under the same lock), so the turn has not stopped at this ticket:
                // advance() skips it when the turn gets here. Waiters behind are unaffected (no wake-up needed).
                state.abandoned.insert(ticket);
                return None;
            }
            state = match self.turn_changed.wait_timeout(state, remaining) {
                Ok((guard, _)) => guard,
                Err(poisoned) => poisoned.into_inner().0,
            };
        }
    }
}

impl Drop for FifoGuard<'_> {
    fn drop(&mut self) {
        let mut state = lock_unpoisoned(&self.lock.state);
        state.advance();
        drop(state);
        self.lock.turn_changed.notify_all();
    }
}

/// Writer thread (L2): owns the Core's stdin. Callers only queue lines, so a Core that stops reading can never
/// block a caller inside a write (keeping the turn forever) nor shutdown(). The thread ends when the bridge
/// drops the sender (shutdown) or a write fails; dropping `stdin` then closes the Core's input (EOF).
fn spawn_request_writer(mut stdin: ChildStdin) -> Option<Sender<String>> {
    let (tx, rx) = mpsc::channel::<String>();
    std::thread::Builder::new()
        .name("core-ipc-writer".into())
        .spawn(move || {
            for line in rx {
                if stdin.write_all(line.as_bytes()).and_then(|_| stdin.flush()).is_err() {
                    break;
                }
            }
        })
        .ok()
        .map(|_| tx)
}

/// Reader thread (L2): owns the Core's stdout and forwards every line. It ends at the end of the stream (the
/// Core exited) or on a read error, dropping the sender, so waiting and later exchanges see "disconnected".
/// A line that is not UTF-8 cannot be a response and is skipped. Nothing is logged.
fn spawn_response_reader(stdout: ChildStdout) -> Option<Receiver<String>> {
    let (tx, rx) = mpsc::channel::<String>();
    std::thread::Builder::new()
        .name("core-ipc-reader".into())
        .spawn(move || {
            let mut reader = BufReader::new(stdout);
            let mut buf = Vec::new();
            loop {
                buf.clear();
                match reader.read_until(b'\n', &mut buf) {
                    Ok(0) => break,
                    Ok(_) => {
                        if let Ok(line) = std::str::from_utf8(&buf) {
                            if tx.send(line.to_owned()).is_err() {
                                break; // the bridge is gone
                            }
                        }
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                    Err(_) => break,
                }
            }
        })
        .ok()
        .map(|_| rx)
}

/// The response to request `id` when `line` is one: a JSON object whose "id" is this request's id. The bridge
/// sends the id as a string and the Core echoes it as received; a number with the same value is accepted too.
/// Anything else (late answer to a call that timed out, null id, notification, not JSON) is None.
fn response_for(line: &str, id: u64, id_text: &str) -> Option<Value> {
    let resp: Value = serde_json::from_str(line.trim()).ok()?;
    let ours = match resp.get("id") {
        Some(Value::String(text)) => text == id_text,
        Some(Value::Number(number)) => number.as_u64() == Some(id),
        _ => false,
    };
    if ours {
        Some(resp)
    } else {
        None
    }
}

pub struct IpcBridge {
    child: Mutex<Option<Child>>,
    // Request lines for the writer thread; None when the Core never started or after shutdown().
    requests: Mutex<Option<Sender<String>>>,
    // Response lines from the reader thread; only the holder of io_lock receives.
    responses: Mutex<Option<Receiver<String>>>,
    io_lock: FifoLock,
    update_frozen: AtomicBool,
    // Set once the Core Python runtime was deliberately stopped for installation.
    // From then on the only valid recovery is restarting the app (see updater_restart_app).
    runtime_stopped_for_update: AtomicBool,
    // Host-side mutual exclusion: at most one updater_download_and_install runs at a time.
    update_in_progress: AtomicBool,
    token: String,
    seq: AtomicU64,
    resolved_python: String,
}

impl IpcBridge {
    pub fn new() -> Self {
        // Issue A: Ephemeral CSPRNG token generated via uuid v4
        let token = format!("desktop-token-{}", uuid::Uuid::new_v4());
        let (py_path_str, child_proc, requests, responses) = match Self::resolve_paths() {
            Ok((py_path, script_path)) => {
                let mut cmd = Command::new(&py_path);
                cmd.arg("-I")
                    .arg(&script_path)
                    .env("COORDINATOR_IPC_TOKEN", &token)
                    .stdin(Stdio::piped())
                    .stdout(Stdio::piped())
                    .stderr(Stdio::piped());

                #[cfg(windows)]
                cmd.creation_flags(CREATE_NO_WINDOW);

                let mut proc = None;
                let mut cin = None;
                let mut cout = None;

                match cmd.spawn() {
                    Ok(mut child) => {
                        if let Some(cin_handle) = child.stdin.take() {
                            cin = spawn_request_writer(cin_handle);
                        }
                        if let Some(cout_handle) = child.stdout.take() {
                            cout = spawn_response_reader(cout_handle);
                        }
                        if let Some(cerr_handle) = child.stderr.take() {
                            std::thread::spawn(move || {
                                let reader = BufReader::new(cerr_handle);
                                for line in reader.lines() {
                                    if let Ok(l) = line {
                                        eprintln!("[PY_IPC_DIAG] {}", l);
                                    }
                                }
                            });
                        }
                        proc = Some(child);
                    }
                    Err(e) => {
                        eprintln!("[TAURI_IPC_WARN] Failed to spawn Python IPC server: {}", e);
                    }
                }
                (py_path.to_string_lossy().to_string(), proc, cin, cout)
            }
            Err(e) => {
                // Issue B: Fail-closed logging when private runtime resolution fails
                eprintln!("[TAURI_IPC_FATAL] {}", e);
                (format!("UNAVAILABLE: {}", e), None, None, None)
            }
        };

        IpcBridge {
            child: Mutex::new(child_proc),
            requests: Mutex::new(requests),
            responses: Mutex::new(responses),
            io_lock: FifoLock::new(),
            update_frozen: AtomicBool::new(false),
            runtime_stopped_for_update: AtomicBool::new(false),
            update_in_progress: AtomicBool::new(false),
            token,
            seq: AtomicU64::new(1),
            resolved_python: py_path_str,
        }
    }

    fn resolve_paths() -> Result<(PathBuf, PathBuf), String> {
        let exe_dir = std::env::current_exe()
            .ok()
            .and_then(|p| p.parent().map(|p| p.to_path_buf()))
            .unwrap_or_else(|| PathBuf::from("."));

        // 1. Resolve Python executable (Issue B: Fail-closed in release mode)
        let py_path = if let Ok(custom) = std::env::var("COORDINATOR_PYTHON") {
            let p = PathBuf::from(custom);
            if !p.exists() {
                return Err(format!("COORDINATOR_PYTHON path does not exist: {}", p.display()));
            }
            p
        } else {
            let candidates = [
                exe_dir.join("runtime").join("python").join("python.exe"),
                exe_dir.join("python").join("python.exe"),
                exe_dir.join("resources").join("runtime").join("python").join("python.exe"),
                exe_dir.join("resources").join("python").join("python.exe"),
                exe_dir.join("..").join("runtime").join("python").join("python.exe"),
                exe_dir.join("..").join("desktop").join("runtime").join("python").join("python.exe"),
                exe_dir.join("..").join("..").join("runtime").join("python").join("python.exe"),
                exe_dir.join("..").join("..").join("..").join("runtime").join("python").join("python.exe"),
                exe_dir.join("..").join("..").join("..").join("desktop").join("runtime").join("python").join("python.exe"),
                PathBuf::from("desktop/runtime/python/python.exe"),
                PathBuf::from("runtime/python/python.exe"),
            ];

            if let Some(found) = candidates.into_iter().find(|p| p.exists()) {
                found
            } else if std::env::var("COORDINATOR_DEV_MODE").as_deref() == Ok("1") {
                eprintln!(
                    "[TAURI_IPC_WARN] Bundled Python runtime not found. Falling back to host PATH 'python' (explicit COORDINATOR_DEV_MODE=1)."
                );
                PathBuf::from("python")
            } else {
                return Err(
                    "Fail-closed: Private bundled Python runtime not found in application resources, and COORDINATOR_PYTHON not set. Refusing to fallback to host PATH python.".to_string(),
                );
            }
        };

        // 2. Resolve Server script
        let script_path = if let Ok(custom_script) = std::env::var("COORDINATOR_SERVER_SCRIPT") {
            let p = PathBuf::from(custom_script);
            if !p.exists() {
                return Err(format!("COORDINATOR_SERVER_SCRIPT path does not exist: {}", p.display()));
            }
            p
        } else {
            let candidates = [
                exe_dir.join("resources").join("desktop").join("ipc").join("server.py"),
                exe_dir.join("resources").join("ipc").join("server.py"),
                exe_dir.join("desktop").join("ipc").join("server.py"),
                exe_dir.join("ipc").join("server.py"),
                exe_dir.join("..").join("desktop").join("ipc").join("server.py"),
                exe_dir.join("..").join("..").join("desktop").join("ipc").join("server.py"),
                exe_dir.join("..").join("..").join("..").join("desktop").join("ipc").join("server.py"),
                PathBuf::from("desktop/ipc/server.py"),
                PathBuf::from("../desktop/ipc/server.py"),
                PathBuf::from("ipc/server.py"),
                PathBuf::from("server.py"),
            ];
            candidates
                .into_iter()
                .find(|p| p.exists())
                .unwrap_or_else(|| PathBuf::from("desktop/ipc/server.py"))
        };

        Ok((py_path, script_path))
    }

    /// One request/response exchange with the Core IPC; returns the whole parsed response (result or error).
    /// The caller holds io_lock. `deadline` ends the call's wait (L2); it was taken when the call started, before
    /// the wait for the turn, so it covers ipc_timeout_for(method) in all. Nothing is sent once it has passed (a
    /// turn that came together with the deadline). Waits until then for the line carrying this request's id and
    /// discards every other line. On timeout the turn is released by the caller and a later exchange drops the
    /// late answer when it arrives.
    fn exchange(&self, method: &str, params: Value, deadline: Instant) -> Result<Value, IpcFailure> {
        let id = self.seq.fetch_add(1, Ordering::SeqCst);
        let id_text = id.to_string();
        let req = json!({
            "jsonrpc": "2.0",
            "id": &id_text,
            "method": method,
            "params": params,
            "session_token": &self.token
        });

        let mut line = serde_json::to_string(&req)
            .map_err(|_| IpcFailure::Unavailable("Falha ao preparar a requisição para o Core."))?;
        if line.len() > IPC_MAX_REQUEST_BYTES {
            return Err(IpcFailure::PayloadTooLarge);
        }
        line.push('\n');

        // Lock order: io_lock (caller) -> responses -> requests. shutdown() takes only requests, briefly.
        let responses = lock_unpoisoned(&self.responses);
        let Some(rx) = responses.as_ref() else {
            return Err(IpcFailure::Unavailable(IPC_CORE_NOT_RUNNING));
        };

        // No time left to wait for an answer: the request is not sent (the Core never sees it).
        if Instant::now() >= deadline {
            return Err(self.deadline_failure(method));
        }

        // 1. Queue the request line for the writer thread (never blocks).
        {
            let requests = lock_unpoisoned(&self.requests);
            let Some(tx) = requests.as_ref() else {
                return Err(IpcFailure::Unavailable(IPC_CORE_NOT_RUNNING));
            };
            if tx.send(line).is_err() {
                // The writer thread ended: the Core's input is closed.
                return Err(IpcFailure::Disconnected);
            }
        }

        // 2. Wait until the deadline for the response with this id; other lines are dropped unread.
        loop {
            match rx.recv_timeout(deadline.saturating_duration_since(Instant::now())) {
                Ok(candidate) => {
                    if let Some(resp) = response_for(&candidate, id, &id_text) {
                        return Ok(resp);
                    }
                }
                Err(RecvTimeoutError::Timeout) => return Err(self.deadline_failure(method)),
                Err(RecvTimeoutError::Disconnected) => return Err(IpcFailure::Disconnected),
            }
        }
    }

    /// Failure of a call whose deadline passed, waiting for the turn or for the response: Timeout with the
    /// method's full wait (the "N s" of the message). A Core that exited while another process still holds its
    /// stdout never closes the stream, so a Core that is down reports Disconnected instead.
    fn deadline_failure(&self, method: &str) -> IpcFailure {
        if self.core_runtime_down() {
            IpcFailure::Disconnected
        } else {
            IpcFailure::Timeout(ipc_timeout_for(method).as_secs())
        }
    }

    /// Exchange under a turn already held, with a deadline that starts now (update freeze).
    fn call_locked(&self, method: &str, params: Value) -> Result<Value, String> {
        self.call_locked_until(method, params, ipc_deadline_for(method))
    }

    /// Exchange under a turn already held, until the deadline the caller took when its call started.
    fn call_locked_until(&self, method: &str, params: Value, deadline: Instant) -> Result<Value, String> {
        let resp = self
            .exchange(method, params, deadline)
            .map_err(|failure| failure.message())?;
        if let Some(err) = resp.get("error") {
            if !err.is_null() {
                let msg = err
                    .get("message")
                    .and_then(|m| m.as_str())
                    .unwrap_or("Unknown IPC error");
                return Err(msg.to_string());
            }
        }

        Ok(resp.get("result").cloned().unwrap_or(Value::Null))
    }

    /// Like `call`, but keeps the Core's structured error object ({code, message, data}) so the UI can show
    /// which field was rejected and why (decision D3). Transport failures become an INTERNAL_ERROR object.
    pub fn call_structured(&self, method: &str, params: Value) -> Result<Value, Value> {
        // L2: the deadline starts before the wait for the turn (see FifoLock::lock_until).
        let deadline = ipc_deadline_for(method);
        let Some(_io_guard) = self.io_lock.lock_until(deadline) else {
            return Err(self.deadline_failure(method).to_structured());
        };
        let resp = self
            .exchange(method, params, deadline)
            .map_err(|failure| failure.to_structured())?;
        if let Some(err) = resp.get("error") {
            if !err.is_null() {
                return Err(err.clone());
            }
        }
        Ok(resp.get("result").cloned().unwrap_or(Value::Null))
    }

    pub fn call(&self, method: &str, params: Value) -> Result<Value, String> {
        // L2: the deadline starts before the wait for the turn (see FifoLock::lock_until).
        let deadline = ipc_deadline_for(method);
        let Some(_io_guard) = self.io_lock.lock_until(deadline) else {
            return Err(self.deadline_failure(method).message());
        };

        if self.update_frozen.load(Ordering::SeqCst)
            && matches!(
                method,
                "terminal.create_tab" | "terminal.create_session" | "terminal.restore_sessions"
            )
        {
            return Err(
                "Criação de sessões bloqueada: sistema congelado para atualização em andamento."
                    .into(),
            );
        }

        self.call_locked_until(method, params, deadline)
    }

    /// Freezes session creation (host flag and Core) under io_lock and counts the sessions. Takes an owned Arc so
    /// it can run on the blocking pool (L1) and hand back a guard that outlives the call.
    fn acquire_update_freeze(self: Arc<Self>) -> (UpdateFreezeGuard, SessionInspection) {
        let _io_guard = self.io_lock.lock();

        self.update_frozen.store(true, Ordering::SeqCst);

        match self.call_locked("system.freeze_for_update", json!({})) {
            Ok(val) => {
                if val.get("frozen").and_then(|f| f.as_bool()) != Some(true) {
                    self.update_frozen.store(false, Ordering::SeqCst);
                    let _ = self.call_locked("system.unfreeze_update", json!({}));
                    return (
                        UpdateFreezeGuard {
                            bridge: Arc::clone(&self),
                            committed: true,
                        },
                        SessionInspection::Unknown(
                            "Core IPC não confirmou estado congelado (frozen != true).".into(),
                        ),
                    );
                }

                let insp = parse_tabs_inspection(&val);
                if matches!(insp, SessionInspection::Unknown(_)) {
                    self.update_frozen.store(false, Ordering::SeqCst);
                    let _ = self.call_locked("system.unfreeze_update", json!({}));
                    return (
                        UpdateFreezeGuard {
                            bridge: Arc::clone(&self),
                            committed: true,
                        },
                        insp,
                    );
                }

                (
                    UpdateFreezeGuard {
                        bridge: Arc::clone(&self),
                        committed: false,
                    },
                    insp,
                )
            }
            Err(e) => {
                self.update_frozen.store(false, Ordering::SeqCst);
                let _ = self.call_locked("system.unfreeze_update", json!({}));
                (
                    UpdateFreezeGuard {
                        bridge: Arc::clone(&self),
                        committed: true,
                    },
                    SessionInspection::Unknown(format!(
                        "Falha ao congelar sessões para atualização: {}",
                        e
                    )),
                )
            }
        }
    }

    /// True when the Core runtime can no longer serve terminal sessions: stopped for an update,
    /// never started, or the owned child process has exited. Unknown states (poisoned lock,
    /// try_wait error) report false, so the restart path stays refused (fail-closed).
    fn core_runtime_down(&self) -> bool {
        if self.runtime_stopped_for_update.load(Ordering::SeqCst) {
            return true;
        }
        match self.child.lock() {
            Ok(mut guard) => match guard.as_mut() {
                None => true,
                Some(child) => matches!(child.try_wait(), Ok(Some(_))),
            },
            Err(_) => false,
        }
    }

    fn begin_update(&self) -> Option<UpdateInProgressGuard<'_>> {
        self.update_in_progress
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .ok()
            .map(|_| UpdateInProgressGuard { bridge: self })
    }

    pub fn shutdown(&self) {
        // Step 1: Close the request channel: the writer thread ends and drops the Core's stdin, sending EOF to
        // the Python IPC server. Never waits on a write in progress (the writer thread owns the pipe).
        let _ = lock_unpoisoned(&self.requests).take();

        // Step 2: Gracefully wait for Python process to terminate its sessions via finally block
        if let Ok(mut guard) = self.child.lock() {
            if let Some(mut child) = guard.take() {
                let mut exited = false;
                for _ in 0..10 {
                    if let Ok(Some(_)) = child.try_wait() {
                        exited = true;
                        break;
                    }
                    std::thread::sleep(std::time::Duration::from_millis(50));
                }
                if !exited {
                    // Safe termination: terminates only the specific child process handle owned by this host,
                    // avoiding generic external process kill commands by numeric PID which could risk killing an unrelated reused PID.
                    let _ = child.kill();
                    let _ = child.wait();
                }
            }
        }
    }
}

// Owns an Arc of the bridge so it can move into the blocking pool (L1) between the freeze and its release.
struct UpdateFreezeGuard {
    bridge: Arc<IpcBridge>,
    committed: bool,
}

impl UpdateFreezeGuard {
    fn commit(mut self) {
        self.committed = true;
    }

    /// Explicit release for gate failures: the unfreeze result is returned to the caller
    /// (Drop can only log it, and logs go nowhere in a windows-subsystem app).
    fn release(mut self) -> Result<(), String> {
        self.committed = true;
        self.bridge.update_frozen.store(false, Ordering::SeqCst);
        self.bridge.call("system.unfreeze_update", json!({})).map(|_| ())
    }
}

struct UpdateInProgressGuard<'a> {
    bridge: &'a IpcBridge,
}

impl<'a> Drop for UpdateInProgressGuard<'a> {
    fn drop(&mut self) {
        self.bridge.update_in_progress.store(false, Ordering::SeqCst);
    }
}

impl Drop for UpdateFreezeGuard {
    fn drop(&mut self) {
        if !self.committed {
            self.bridge.update_frozen.store(false, Ordering::SeqCst);
            if let Err(e) = self.bridge.call("system.unfreeze_update", json!({})) {
                eprintln!("[UPDATER_FREEZE_WARN] Falha ao descongelar sessões no Core Python: {}", e);
            }
        }
    }
}

// Issue C: Synchronized process tree termination on window close
impl Drop for IpcBridge {
    fn drop(&mut self) {
        self.shutdown();
    }
}

#[tauri::command]
fn ping() -> &'static str {
    "pong"
}

// L1: every command below that reaches the Core is async and runs the blocking call on the blocking pool with
// an Arc of the bridge (tauri::async_runtime::spawn_blocking), so the main (window) thread never waits for the
// Core. A task that does not finish becomes the command's own error type, with a fixed text.

// Async commands with borrowed state must return a Result (Tauri 2). It is always Ok: the interface receives
// the same RuntimeStatus as before, and a Core that cannot be reached reads "indisponível".
#[tauri::command]
async fn get_runtime_status(bridge: State<'_, Arc<IpcBridge>>) -> Result<RuntimeStatus, String> {
    let is_isolated = !bridge.resolved_python.starts_with("UNAVAILABLE");
    let python_path = bridge.resolved_python.clone();
    // Core and Python versions are reported by the running Core (system.status), never hard-coded;
    // app version and license come from this build's Cargo metadata.
    let bridge = bridge.inner().clone();
    let core_status = tauri::async_runtime::spawn_blocking(move || bridge.call("system.status", json!({})).ok())
        .await
        .ok()
        .flatten();
    let core_field = |key: &str| -> Option<String> {
        core_status
            .as_ref()
            .and_then(|v| v.get(key))
            .and_then(|v| v.as_str())
            .map(String::from)
    };
    Ok(RuntimeStatus {
        core_version: core_field("version").unwrap_or_else(|| "indisponível".into()),
        python_version: core_field("python_version"),
        app_version: env!("CARGO_PKG_VERSION").into(),
        license: env!("CARGO_PKG_LICENSE").into(),
        host_os: std::env::consts::OS.into(),
        host_arch: std::env::consts::ARCH.into(),
        is_isolated,
        python_path,
    })
}

// Issue D: Recursively measure all process tree descendants (children, grandchildren, WebView2 renderers)
#[tauri::command]
async fn get_process_tree_metrics() -> Result<ProcessTreeMetrics, String> {
    // Walking every process takes 50-235 ms: off the main thread, or the window stutters every 2 s (Etapa 8, L1).
    tauri::async_runtime::spawn_blocking(measure_process_tree)
        .await
        .map_err(|_| IPC_TASK_FAILED_MESSAGE.to_string())
}

fn measure_process_tree() -> ProcessTreeMetrics {
    let mut sys = System::new();
    let current_pid = Pid::from_u32(std::process::id());

    sys.refresh_processes_specifics(ProcessRefreshKind::everything());

    let mut tree_pids = HashSet::new();
    tree_pids.insert(current_pid);

    // Iteratively collect all descendant PIDs until fixed point
    let mut added = true;
    while added {
        added = false;
        for (pid, proc) in sys.processes() {
            if !tree_pids.contains(pid) {
                if let Some(parent) = proc.parent() {
                    if tree_pids.contains(&parent) {
                        tree_pids.insert(*pid);
                        added = true;
                    }
                }
            }
        }
    }

    let mut total_ram: u64 = 0;
    let mut host_ram: u64 = 0;
    let mut total_cpu: f32 = 0.0;

    if let Some(host_proc) = sys.process(current_pid) {
        host_ram = host_proc.memory();
    }

    for pid in &tree_pids {
        if let Some(proc) = sys.process(*pid) {
            total_ram += proc.memory();
            total_cpu += proc.cpu_usage();
        }
    }

    ProcessTreeMetrics {
        host_pid: std::process::id(),
        host_ram_bytes: host_ram,
        total_tree_ram_bytes: total_ram,
        cpu_usage_percent: total_cpu,
    }
}

// L3: structured errors ({code, message, data}) like the settings commands.
#[tauri::command]
async fn profile_get_active(bridge: State<'_, Arc<IpcBridge>>) -> Result<Value, Value> {
    let bridge = bridge.inner().clone();
    tauri::async_runtime::spawn_blocking(move || bridge.call_structured("profile.get_active", json!({})))
        .await
        .unwrap_or_else(|_| Err(ipc_task_failed_structured()))
}

// L3: structured errors; `confirm_broad` (interface key confirmBroad) authorizes a broad directory (L5).
#[tauri::command]
async fn profile_authorize_project(
    project_path: String,
    confirm_broad: Option<bool>,
    bridge: State<'_, Arc<IpcBridge>>,
) -> Result<Value, Value> {
    let bridge = bridge.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        bridge.call_structured(
            "profile.authorize_project",
            json!({
                "project_path": project_path,
                "confirm_broad": confirm_broad
            }),
        )
    })
    .await
    .unwrap_or_else(|_| Err(ipc_task_failed_structured()))
}

#[tauri::command]
async fn profile_list_authorized_projects(bridge: State<'_, Arc<IpcBridge>>) -> Result<Value, String> {
    let bridge = bridge.inner().clone();
    tauri::async_runtime::spawn_blocking(move || bridge.call("profile.list_authorized_projects", json!({})))
        .await
        .unwrap_or_else(|_| Err(ipc_task_failed()))
}

#[tauri::command]
async fn terminal_create_tab(
    tab_id: String,
    project_path: String,
    bridge: State<'_, Arc<IpcBridge>>,
) -> Result<Value, String> {
    let bridge = bridge.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        bridge.call(
            "terminal.create_tab",
            json!({
                "tab_id": tab_id,
                "project_path": project_path
            }),
        )
    })
    .await
    .unwrap_or_else(|_| Err(ipc_task_failed()))
}

#[tauri::command]
async fn terminal_create_session(
    tab_id: String,
    pane_id: u32,
    kind: String,
    // argv list (program + arguments); a plain string is rejected by deserialization and by the Core IPC.
    command: Option<Vec<String>>,
    interpreter_id: Option<String>,
    cwd: Option<String>,
    cols: Option<u16>,
    rows: Option<u16>,
    bridge: State<'_, Arc<IpcBridge>>,
) -> Result<Value, String> {
    let bridge = bridge.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        bridge.call(
            "terminal.create_session",
            json!({
                "tab_id": tab_id,
                "pane_id": pane_id,
                "kind": kind,
                "command": command,
                "interpreter_id": interpreter_id,
                "cwd": cwd,
                "cols": cols.unwrap_or(80),
                "rows": rows.unwrap_or(24)
            }),
        )
    })
    .await
    .unwrap_or_else(|_| Err(ipc_task_failed()))
}

#[tauri::command]
async fn terminal_list_interpreters(bridge: State<'_, Arc<IpcBridge>>) -> Result<Value, String> {
    let bridge = bridge.inner().clone();
    tauri::async_runtime::spawn_blocking(move || bridge.call("terminal.list_interpreters", json!({})))
        .await
        .unwrap_or_else(|_| Err(ipc_task_failed()))
}

#[tauri::command]
async fn terminal_validate_interpreter(
    interpreter_id: String,
    cwd: String,
    bridge: State<'_, Arc<IpcBridge>>,
) -> Result<Value, String> {
    let bridge = bridge.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        bridge.call("terminal.validate_interpreter", json!({ "interpreter_id": interpreter_id, "cwd": cwd }))
    })
    .await
    .unwrap_or_else(|_| Err(ipc_task_failed()))
}

#[tauri::command]
async fn terminal_write(
    session_id: String,
    data: String,
    bridge: State<'_, Arc<IpcBridge>>,
) -> Result<Value, String> {
    let bridge = bridge.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        bridge.call(
            "terminal.write",
            json!({
                "session_id": session_id,
                "data": data
            }),
        )
    })
    .await
    .unwrap_or_else(|_| Err(ipc_task_failed()))
}

#[tauri::command]
async fn terminal_resize(
    session_id: String,
    cols: u16,
    rows: u16,
    bridge: State<'_, Arc<IpcBridge>>,
) -> Result<Value, String> {
    let bridge = bridge.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        bridge.call(
            "terminal.resize",
            json!({
                "session_id": session_id,
                "cols": cols,
                "rows": rows
            }),
        )
    })
    .await
    .unwrap_or_else(|_| Err(ipc_task_failed()))
}

#[tauri::command]
async fn terminal_get_output(
    session_id: String,
    since_seq: Option<u64>,
    bridge: State<'_, Arc<IpcBridge>>,
) -> Result<Value, String> {
    let bridge = bridge.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        bridge.call(
            "terminal.get_output",
            json!({
                "session_id": session_id,
                "since_seq": since_seq.unwrap_or(0)
            }),
        )
    })
    .await
    .unwrap_or_else(|_| Err(ipc_task_failed()))
}

#[tauri::command]
async fn terminal_close_session(
    session_id: String,
    bridge: State<'_, Arc<IpcBridge>>,
) -> Result<Value, String> {
    let bridge = bridge.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        bridge.call(
            "terminal.close_session",
            json!({
                "session_id": session_id
            }),
        )
    })
    .await
    .unwrap_or_else(|_| Err(ipc_task_failed()))
}

#[tauri::command]
async fn terminal_save_sessions(
    tab_id: String,
    bridge: State<'_, Arc<IpcBridge>>,
) -> Result<Value, String> {
    let bridge = bridge.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        bridge.call(
            "terminal.save_sessions",
            json!({
                "tab_id": tab_id
            }),
        )
    })
    .await
    .unwrap_or_else(|_| Err(ipc_task_failed()))
}

#[tauri::command]
async fn terminal_list_tabs(bridge: State<'_, Arc<IpcBridge>>) -> Result<Value, String> {
    let bridge = bridge.inner().clone();
    tauri::async_runtime::spawn_blocking(move || bridge.call("terminal.list_tabs", json!({})))
        .await
        .unwrap_or_else(|_| Err(ipc_task_failed()))
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct UpdateCheckResult {
    pub available: bool,
    pub current_version: String,
    pub latest_version: String,
    pub notes: String,
    pub date: String,
    pub active_sessions_count: usize,
    pub sessions_known: bool,
    pub sessions_error: Option<String>,
    pub offline: bool,
    pub error: Option<String>,
    // Core runtime is down (stopped for an update or exited): the UI must offer only the restart path.
    pub runtime_stopped: bool,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct UpdaterChannelInfo {
    pub current_version: String,
    pub channel: String,
    pub endpoint: String,
}

#[derive(Debug, Clone)]
enum SessionInspection {
    Known(usize),
    Unknown(String),
}

fn parse_tabs_inspection(val: &Value) -> SessionInspection {
    let tabs = match val.get("tabs").and_then(|t| t.as_array()) {
        Some(t) => t,
        None => return SessionInspection::Unknown("Resposta inválida do Core IPC: campo 'tabs' ausente ou não é array.".into()),
    };
    let mut total: usize = 0;
    for tab in tabs {
        let from_panes: Option<usize> = match tab.get("panes") {
            Some(serde_json::Value::Object(m)) => Some(m.len()),
            Some(serde_json::Value::Array(a)) => Some(a.len()),
            _ => None,
        };
        let explicit = tab.get("active_panes_count").and_then(|v| v.as_u64())
            .or_else(|| tab.get("panes_count").and_then(|v| v.as_u64()))
            .map(|n| n as usize);

        let n = match (from_panes, explicit) {
            (Some(a), Some(b)) => a.max(b),
            (Some(a), None) | (None, Some(a)) => a,
            (None, None) => return SessionInspection::Unknown("Aba retornada pelo Core sem 'panes' ou 'panes_count'.".into()),
        };
        total = match total.checked_add(n) {
            Some(t) => t,
            None => return SessionInspection::Unknown("Estouro numérico ao somar sessões ativas.".into()),
        };
    }
    SessionInspection::Known(total)
}

fn count_active_sessions(bridge: &IpcBridge) -> SessionInspection {
    let val = match bridge.call("terminal.list_tabs", json!({})) {
        Ok(v) => v,
        Err(e) => return SessionInspection::Unknown(format!("Falha de comunicação com Core IPC: {}", e)),
    };
    parse_tabs_inspection(&val)
}

enum InstallGate {
    Allow,
    Block(String),
}

fn evaluate_gate(
    insp: SessionInspection,
    force_update: bool,
    expected_active_sessions: Option<usize>,
) -> InstallGate {
    match insp {
        SessionInspection::Unknown(why) => {
            InstallGate::Block(format!(
                "Segurança fail-closed: Estado das sessões de terminal é indefinido/desconhecido ({}). Atualização bloqueada; autorização forçada não se aplica a estados desconhecidos.",
                why
            ))
        }
        SessionInspection::Known(actual) => {
            if actual == 0 {
                if let Some(expected) = expected_active_sessions {
                    if expected != 0 {
                        return InstallGate::Block(format!(
                            "A contagem de sessões mudou de {} para 0 desde a sua autorização (sessions_changed). Confirmação anterior invalidada; verifique novamente.",
                            expected
                        ));
                    }
                }
                InstallGate::Allow
            } else if !force_update {
                InstallGate::Block(format!(
                    "Existem {} terminais ativos em execução. Conclua suas tarefas ou autorize a atualização forçada com confirmação explícita.",
                    actual
                ))
            } else {
                match expected_active_sessions {
                    Some(expected) if expected == actual => InstallGate::Allow,
                    Some(expected) => InstallGate::Block(format!(
                        "A contagem de sessões mudou de {} para {} desde a sua autorização (sessions_changed). Confirmação anterior invalidada; reautorize para prosseguir.",
                        expected, actual
                    )),
                    None => InstallGate::Block(format!(
                        "Autorização forçada inválida: contagem esperada não foi informada pelo frontend para {} terminais ativos.",
                        actual
                    )),
                }
            }
        }
    }
}

#[tauri::command]
async fn updater_get_channel_info(app: tauri::AppHandle) -> UpdaterChannelInfo {
    let current_version = app.package_info().version.to_string();
    UpdaterChannelInfo {
        current_version,
        channel: "alpha".into(),
        endpoint: "https://raw.githubusercontent.com/MaxxArtes/ai-coordinator/feature/phase4d-m2-terminals/desktop/updates/alpha.json".into(),
    }
}

#[tauri::command]
async fn updater_check(app: tauri::AppHandle, bridge: State<'_, Arc<IpcBridge>>) -> Result<UpdateCheckResult, String> {
    use tauri_plugin_updater::UpdaterExt;

    let current_version = app.package_info().version.to_string();

    // Check active sessions count from Core IPC (fail-closed if check fails), off the main thread (L1)
    let sessions_bridge = bridge.inner().clone();
    let inspection = tauri::async_runtime::spawn_blocking(move || count_active_sessions(&sessions_bridge))
        .await
        .unwrap_or_else(|_| SessionInspection::Unknown(IPC_TASK_FAILED_MESSAGE.to_string()));
    let (sessions_known, active_sessions_count, sessions_error) = match inspection {
        SessionInspection::Known(n) => (true, n, None),
        SessionInspection::Unknown(why) => (false, 0, Some(why)),
    };
    let runtime_stopped = bridge.core_runtime_down();

    let updater = match app.updater() {
        Ok(u) => u,
        Err(e) => {
            return Ok(UpdateCheckResult {
                available: false,
                current_version: current_version.clone(),
                latest_version: current_version,
                notes: String::new(),
                date: String::new(),
                active_sessions_count,
                sessions_known,
                sessions_error,
                runtime_stopped,
                offline: true,
                error: Some(format!("Falha ao inicializar verificador: {}", e)),
            });
        }
    };

    match updater.check().await {
        Ok(Some(update)) => {
            Ok(UpdateCheckResult {
                available: true,
                current_version: current_version.clone(),
                latest_version: update.version.clone(),
                notes: update.body.clone().unwrap_or_else(|| "Notas de lançamento da versão Alpha.".into()),
                date: update.date.map(|d| d.to_string()).unwrap_or_default(),
                active_sessions_count,
                sessions_known,
                sessions_error,
                runtime_stopped,
                offline: false,
                error: None,
            })
        }
        Ok(None) => {
            Ok(UpdateCheckResult {
                available: false,
                current_version: current_version.clone(),
                latest_version: current_version,
                notes: String::new(),
                date: String::new(),
                active_sessions_count,
                sessions_known,
                sessions_error,
                runtime_stopped,
                offline: false,
                error: None,
            })
        }
        Err(e) => {
            // Graceful offline tolerance
            Ok(UpdateCheckResult {
                available: false,
                current_version: current_version.clone(),
                latest_version: current_version,
                notes: String::new(),
                date: String::new(),
                active_sessions_count,
                sessions_known,
                sessions_error,
                runtime_stopped,
                offline: true,
                error: Some(format!("Servidor de atualização inacessível (modo offline): {}", e)),
            })
        }
    }
}

/// Structured updater failure, serialized as the invoke rejection payload.
/// `runtime_stopped` tells the UI whether the Core Python runtime and terminals are still operational.
/// When true, the only safe recovery is `updater_restart_app`.
#[derive(Serialize, Debug, Clone)]
pub struct UpdaterError {
    pub code: &'static str,
    pub message: String,
    pub runtime_stopped: bool,
}

impl UpdaterError {
    /// Failure raised before this command stopped the runtime (nothing was terminated by the updater).
    /// `runtime_stopped` still reflects the real Core state: a Core that died on its own is reported,
    /// so the UI offers the restart path instead of a dead "retry".
    fn before_shutdown(bridge: &IpcBridge, code: &'static str, message: impl Into<String>) -> Self {
        UpdaterError {
            code,
            message: message.into(),
            runtime_stopped: bridge.core_runtime_down(),
        }
    }

    /// Failure after the Core runtime was deliberately stopped for installation.
    fn after_shutdown(code: &'static str, message: impl Into<String>) -> Self {
        UpdaterError {
            code,
            message: message.into(),
            runtime_stopped: true,
        }
    }
}

#[tauri::command]
async fn updater_download_and_install(
    app: tauri::AppHandle,
    bridge: State<'_, Arc<IpcBridge>>,
    force: Option<bool>,
    expected_active_sessions: Option<usize>,
) -> Result<bool, UpdaterError> {
    use tauri_plugin_updater::UpdaterExt;

    // Exclusão mútua no host: uma única instalação por vez, mesmo com invocações concorrentes
    // (o botão desabilitado na UI não impede um segundo invoke). Liberada ao sair da função.
    let _in_progress = match bridge.begin_update() {
        Some(guard) => guard,
        None => {
            return Err(UpdaterError::before_shutdown(
                &bridge,
                "UPDATE_IN_PROGRESS",
                "Já existe uma atualização em andamento. Aguarde a conclusão antes de tentar novamente.",
            ))
        }
    };

    // Uma instalação anterior já encerrou o runtime: nunca presumir Python ativo nem tentar de novo.
    if bridge.runtime_stopped_for_update.load(Ordering::SeqCst) {
        return Err(UpdaterError::after_shutdown(
            "RUNTIME_STOPPED",
            "O runtime Core foi encerrado por uma tentativa de instalação anterior. Reinicie o aplicativo antes de tentar novamente.",
        ));
    }

    let force_update = force.unwrap_or(false);

    // ETAPA 1 (Fail-Closed Pré-Download): Verificar se sessões estão ativas ou se inspeção falhou.
    // As chamadas ao Core desta função rodam no pool de bloqueio (L1); tarefa que não termina = estado desconhecido.
    let sessions_bridge = bridge.inner().clone();
    let pre_insp = tauri::async_runtime::spawn_blocking(move || count_active_sessions(&sessions_bridge))
        .await
        .unwrap_or_else(|_| SessionInspection::Unknown(IPC_TASK_FAILED_MESSAGE.to_string()));
    if let InstallGate::Block(reason) = evaluate_gate(pre_insp, force_update, expected_active_sessions) {
        return Err(UpdaterError::before_shutdown(&bridge, "SESSIONS_GATE_BLOCKED", reason));
    }

    let updater = app.updater().map_err(|e| {
        UpdaterError::before_shutdown(&bridge, "UPDATER_UNAVAILABLE", format!("Falha ao inicializar o atualizador: {}", e))
    })?;
    let update = updater.check().await.map_err(|e| {
        UpdaterError::before_shutdown(&bridge, "CHECK_FAILED", format!("Falha ao consultar atualizações: {}", e))
    })?;

    if let Some(update) = update {
        // ETAPA 2 (Segurança): Baixar e validar a assinatura Ed25519 ANTES de congelar/desligar o runtime Python
        let app_handle_progress = app.clone();
        let mut downloaded: u64 = 0;

        let downloaded_bytes = update
            .download(
                move |chunk_len, total_opt| {
                    downloaded += chunk_len as u64;
                    let total = total_opt.unwrap_or(0);
                    let pct = if total > 0 { (downloaded as f64 / total as f64) * 100.0 } else { 0.0 };
                    let _ = app_handle_progress.emit("updater-progress", json!({
                        "downloaded": downloaded,
                        "total": total,
                        "percent": pct,
                        "status": "downloading"
                    }));
                },
                || {},
            )
            .await
            .map_err(|e| {
                UpdaterError::before_shutdown(
                    &bridge,
                    "DOWNLOAD_OR_SIGNATURE_FAILED",
                    format!("Falha no download/validação da atualização: {}. O aplicativo e seus terminais continuam operacionais.", e),
                )
            })?;

        // ETAPA 3 (Exclusão Mútua / Freeze + Revalidação Pré-Instalação):
        // Congela criação de novas abas/sessões no Rust e no Core Python (sob RLock) antes de contar.
        // Se a validação falhar, o congelamento é liberado explicitamente e uma falha ao descongelar
        // é informada ao usuário; UpdateFreezeGuard::drop permanece como rede de segurança.
        let freeze_bridge = bridge.inner().clone();
        let (freeze_guard, post_insp) =
            match tauri::async_runtime::spawn_blocking(move || freeze_bridge.acquire_update_freeze()).await {
                Ok(frozen) => frozen,
                Err(_) => {
                    // The task stopped (panic) before returning a guard. If it stopped after setting the host
                    // flag, terminal creation stays blocked until the app restarts, hence the advice.
                    return Err(UpdaterError::before_shutdown(
                        &bridge,
                        "PRE_INSTALL_GATE_BLOCKED",
                        format!(
                            "Revalidação pré-instalação bloqueada: {} Se a criação de terminais continuar bloqueada, feche e reabra o aplicativo.",
                            IPC_TASK_FAILED_MESSAGE
                        ),
                    ));
                }
            };
        if let InstallGate::Block(reason) = evaluate_gate(post_insp, force_update, expected_active_sessions) {
            let mut message = format!("Revalidação pré-instalação bloqueada: {}", reason);
            let released = tauri::async_runtime::spawn_blocking(move || freeze_guard.release())
                .await
                .unwrap_or_else(|_| Err(ipc_task_failed()));
            if let Err(e) = released {
                message.push_str(&format!(
                    " Atenção: o Core não confirmou o descongelamento ({}). Se a criação de terminais continuar bloqueada, feche e reabra o aplicativo.",
                    e
                ));
            }
            return Err(UpdaterError::before_shutdown(&bridge, "PRE_INSTALL_GATE_BLOCKED", message));
        }

        // Confirma permanência no estado congelado até o shutdown imediato
        freeze_guard.commit();

        let _ = app.emit("updater-progress", json!({
            "status": "installing",
            "percent": 100.0
        }));

        // ETAPA 4: Apenas após validação sob exclusão mútua passar, encerra o Python e Job Objects.
        // A marca é gravada antes do shutdown: a partir daqui nenhum caminho pode presumir Python ativo.
        bridge.runtime_stopped_for_update.store(true, Ordering::SeqCst);
        bridge.shutdown();

        // ETAPA 5: Instala o payload validado. No Windows, sucesso entrega o controle ao instalador NSIS
        // e encerra este processo; um retorno com erro significa que o instalador não foi iniciado
        // (ex.: falha ao gravar o pacote temporário ou ao iniciar o processo do instalador).
        if let Err(e) = update.install(downloaded_bytes) {
            eprintln!("[UPDATER_INSTALL_FAIL] Falha pós-shutdown: {}", e);
            return Err(UpdaterError::after_shutdown(
                "INSTALL_FAILED_RUNTIME_STOPPED",
                format!(
                    "Falha ao executar o instalador da atualização: {}. O runtime Core e os terminais já haviam sido encerrados com segurança; reinicie o aplicativo para restaurá-los na versão atual.",
                    e
                ),
            ));
        }

        // ETAPA 6: Reinicia o aplicativo preservando configurações e perfis
        app.restart();
        Ok(true)
    } else {
        Err(UpdaterError::before_shutdown(
            &bridge,
            "NO_UPDATE",
            "Nenhuma atualização pendente encontrada para instalação.",
        ))
    }
}

/// Recuperação: reinicia a versão instalada, que sobe um novo runtime Core Python. Só é aceito quando o
/// Core já não atende sessões (encerrado para instalação ou processo filho finalizado) e nenhuma
/// instalação está em andamento; com o Core ativo é recusado, para nunca contornar a cerca de sessões.
#[tauri::command]
fn updater_restart_app(app: tauri::AppHandle, bridge: State<Arc<IpcBridge>>) -> Result<(), String> {
    if bridge.update_in_progress.load(Ordering::SeqCst) {
        return Err("Reinício recusado: há uma atualização em andamento.".into());
    }
    if !bridge.core_runtime_down() {
        return Err(
            "Reinício recusado: o runtime Core está ativo. Use o fluxo de atualização, que verifica as sessões de terminal."
                .into(),
        );
    }
    app.restart();
    Ok(())
}

// ---- Settings and vault (Marco 4D.3, Etapa 3): validation happens in the Core IPC; structured errors
// reach the UI as {code, message, data}. Vault commands never return secret values. ----
#[tauri::command]
async fn settings_get(bridge: State<'_, Arc<IpcBridge>>) -> Result<Value, Value> {
    let bridge = bridge.inner().clone();
    tauri::async_runtime::spawn_blocking(move || bridge.call_structured("settings.get", json!({})))
        .await
        .unwrap_or_else(|_| Err(ipc_task_failed_structured()))
}

#[tauri::command]
async fn settings_update(
    schema_version: u32,
    category: String,
    values: Value,
    bridge: State<'_, Arc<IpcBridge>>,
) -> Result<Value, Value> {
    let bridge = bridge.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        bridge.call_structured(
            "settings.update",
            json!({ "schema_version": schema_version, "category": category, "values": values }),
        )
    })
    .await
    .unwrap_or_else(|_| Err(ipc_task_failed_structured()))
}

#[tauri::command]
async fn vault_status(bridge: State<'_, Arc<IpcBridge>>) -> Result<Value, Value> {
    let bridge = bridge.inner().clone();
    tauri::async_runtime::spawn_blocking(move || bridge.call_structured("vault.status", json!({})))
        .await
        .unwrap_or_else(|_| Err(ipc_task_failed_structured()))
}

#[tauri::command]
async fn vault_list_keys(bridge: State<'_, Arc<IpcBridge>>) -> Result<Value, Value> {
    let bridge = bridge.inner().clone();
    tauri::async_runtime::spawn_blocking(move || bridge.call_structured("vault.list_keys", json!({})))
        .await
        .unwrap_or_else(|_| Err(ipc_task_failed_structured()))
}

#[tauri::command]
async fn vault_set_secret(
    key: String,
    value: String,
    expected_revision: Option<String>,
    bridge: State<'_, Arc<IpcBridge>>,
) -> Result<Value, Value> {
    let bridge = bridge.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        bridge.call_structured(
            "vault.set_secret",
            json!({ "key": key, "value": value, "expected_revision": expected_revision }),
        )
    })
    .await
    .unwrap_or_else(|_| Err(ipc_task_failed_structured()))
}

// L4: `expected_revision` (interface key expectedRevision) is the revision of the operator's own reading; a vault
// changed since then is a conflict and nothing is removed (L6).
#[tauri::command]
async fn vault_delete_secret(
    key: String,
    expected_revision: Option<String>,
    bridge: State<'_, Arc<IpcBridge>>,
) -> Result<Value, Value> {
    let bridge = bridge.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        bridge.call_structured(
            "vault.delete_secret",
            json!({ "key": key, "expected_revision": expected_revision }),
        )
    })
    .await
    .unwrap_or_else(|_| Err(ipc_task_failed_structured()))
}

// Etapa 7: authenticated desktop-layer operations; no Core engine changes.
#[tauri::command]
async fn profile_revoke_project(project_path: String, bridge: State<'_, Arc<IpcBridge>>) -> Result<Value, Value> {
    let bridge = bridge.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        bridge.call_structured("profile.revoke_project", json!({ "project_path": project_path }))
    })
    .await
    .unwrap_or_else(|_| Err(ipc_task_failed_structured()))
}

#[tauri::command]
async fn profile_remember_project(project_path: String, bridge: State<'_, Arc<IpcBridge>>) -> Result<Value, Value> {
    let bridge = bridge.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        bridge.call_structured("profile.remember_project", json!({ "project_path": project_path }))
    })
    .await
    .unwrap_or_else(|_| Err(ipc_task_failed_structured()))
}

#[tauri::command]
async fn security_status(bridge: State<'_, Arc<IpcBridge>>) -> Result<Value, Value> {
    let bridge = bridge.inner().clone();
    tauri::async_runtime::spawn_blocking(move || bridge.call_structured("security.status", json!({})))
        .await
        .unwrap_or_else(|_| Err(ipc_task_failed_structured()))
}

#[tauri::command]
async fn discovery_agents(bridge: State<'_, Arc<IpcBridge>>) -> Result<Value, Value> {
    let bridge = bridge.inner().clone();
    tauri::async_runtime::spawn_blocking(move || bridge.call_structured("discovery.agents", json!({})))
        .await
        .unwrap_or_else(|_| Err(ipc_task_failed_structured()))
}

#[tauri::command]
async fn providers_routes(bridge: State<'_, Arc<IpcBridge>>) -> Result<Value, Value> {
    let bridge = bridge.inner().clone();
    tauri::async_runtime::spawn_blocking(move || bridge.call_structured("providers.routes", json!({})))
        .await
        .unwrap_or_else(|_| Err(ipc_task_failed_structured()))
}

fn main() {
    let bridge = Arc::new(IpcBridge::new());
    let bridge_window = bridge.clone();

    tauri::Builder::default()
        .manage(bridge)
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .setup(|_app| {
            // Windows only: color the "main" title bar; failures are ignored inside the helper.
            #[cfg(windows)]
            apply_main_window_caption_color(_app);
            Ok(())
        })
        .on_window_event(move |_window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                bridge_window.shutdown();
            }
        })
        .invoke_handler(tauri::generate_handler![
            ping,
            get_runtime_status,
            get_process_tree_metrics,
            profile_get_active,
            profile_authorize_project,
            profile_list_authorized_projects,
            terminal_create_tab,
            terminal_create_session,
            terminal_list_interpreters,
            terminal_validate_interpreter,
            terminal_write,
            terminal_resize,
            terminal_get_output,
            terminal_close_session,
            terminal_save_sessions,
            terminal_list_tabs,
            updater_get_channel_info,
            updater_check,
            updater_download_and_install,
            updater_restart_app,
            settings_get,
            settings_update,
            vault_status,
            vault_list_keys,
            vault_set_secret,
            vault_delete_secret,
            profile_revoke_project,
            profile_remember_project,
            security_status,
            discovery_agents,
            providers_routes
        ])
        .run(tauri::generate_context!())
        .expect("error while running AI Coordinator Desktop");
}

// Transport tests (L2 and the queue deadline of Etapa 8). They touch no Tauri API: the Verify suite cannot build
// this binary, so they also run from a crate that compiles this transport section alone.
#[cfg(test)]
mod ipc_transport_tests {
    use super::*;
    use std::thread;

    // Scheduling margin on top of a deadline.
    const SLACK: Duration = Duration::from_millis(400);

    fn wait_for_tickets(lock: &FifoLock, tickets: u64) {
        let start = Instant::now();
        while lock_unpoisoned(&lock.state).next_ticket < tickets {
            assert!(start.elapsed() < Duration::from_secs(5), "as chamadas não entraram na fila");
            thread::sleep(Duration::from_millis(2));
        }
    }

    fn live_child() -> Child {
        #[cfg(windows)]
        let mut cmd = {
            let mut c = Command::new("ping");
            c.args(["-n", "60", "127.0.0.1"]);
            c
        };
        #[cfg(not(windows))]
        let mut cmd = {
            let mut c = Command::new("sleep");
            c.arg("60");
            c
        };
        cmd.stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("processo que faz as vezes do Core")
    }

    /// A bridge wired to channels instead of a Core process: the test reads the request lines and writes replies.
    fn fake_bridge(child: Option<Child>) -> (IpcBridge, Receiver<String>, Sender<String>) {
        let (request_tx, request_rx) = mpsc::channel::<String>();
        let (reply_tx, reply_rx) = mpsc::channel::<String>();
        let bridge = IpcBridge {
            child: Mutex::new(child),
            requests: Mutex::new(Some(request_tx)),
            responses: Mutex::new(Some(reply_rx)),
            io_lock: FifoLock::new(),
            update_frozen: AtomicBool::new(false),
            runtime_stopped_for_update: AtomicBool::new(false),
            update_in_progress: AtomicBool::new(false),
            token: "token-de-teste".into(),
            seq: AtomicU64::new(1),
            resolved_python: String::new(),
        };
        (bridge, request_rx, reply_tx)
    }

    /// Answers each request after `delay` with {"method": <its method>} and the request's id, as the Core does.
    fn answer_after(requests: Receiver<String>, replies: Sender<String>, delay: Duration) -> thread::JoinHandle<()> {
        thread::spawn(move || {
            for request in requests {
                let request: Value = serde_json::from_str(request.trim()).expect("pedido em JSON");
                thread::sleep(delay);
                let reply = json!({
                    "jsonrpc": "2.0",
                    "id": request["id"].clone(),
                    "result": { "method": request["method"].clone() }
                });
                if replies.send(format!("{}\n", reply)).is_err() {
                    break;
                }
            }
        })
    }

    #[test]
    fn lock_until_gives_up_at_its_deadline_while_the_turn_is_held() {
        let lock = FifoLock::new();
        let _held = lock.lock();
        let start = Instant::now();
        assert!(lock.lock_until(start + Duration::from_millis(200)).is_none());
        let waited = start.elapsed();
        assert!(waited >= Duration::from_millis(200) && waited < Duration::from_millis(200) + SLACK, "{waited:?}");
    }

    #[test]
    fn a_free_turn_is_kept_even_when_the_deadline_has_passed() {
        let lock = FifoLock::new();
        let past = Instant::now();
        thread::sleep(Duration::from_millis(5));
        let first = lock.lock_until(past);
        assert!(first.is_some());
        drop(first);
        assert!(lock.lock_until(past).is_some());
        assert!(lock_unpoisoned(&lock.state).abandoned.is_empty());
    }

    #[test]
    fn abandoned_tickets_are_skipped_when_the_turn_moves_on() {
        let lock = FifoLock::new();
        let held = lock.lock();
        assert!(lock.lock_until(Instant::now() + Duration::from_millis(30)).is_none());
        assert!(lock.lock_until(Instant::now() + Duration::from_millis(30)).is_none());
        assert_eq!(lock_unpoisoned(&lock.state).abandoned.len(), 2);
        drop(held);
        let start = Instant::now();
        let next = lock.lock_until(start + Duration::from_secs(5));
        assert!(next.is_some() && start.elapsed() < SLACK);
        assert!(lock_unpoisoned(&lock.state).abandoned.is_empty());
    }

    #[test]
    fn callers_still_waiting_keep_their_arrival_order() {
        let lock = Arc::new(FifoLock::new());
        let order = Arc::new(Mutex::new(Vec::new()));
        let held = lock.lock();
        let mut waiting = Vec::new();
        for caller in 0..5u64 {
            let (queue, served) = (Arc::clone(&lock), Arc::clone(&order));
            // Caller 2 gives up early; the others wait long enough.
            let patience = if caller == 2 { Duration::from_millis(50) } else { Duration::from_secs(10) };
            waiting.push(thread::spawn(move || {
                if let Some(_turn) = queue.lock_until(Instant::now() + patience) {
                    lock_unpoisoned(&served).push(caller);
                }
            }));
            wait_for_tickets(&lock, caller + 2);
        }
        thread::sleep(Duration::from_millis(150));
        drop(held);
        for caller in waiting {
            caller.join().expect("chamada em espera");
        }
        assert_eq!(*lock_unpoisoned(&order), vec![0, 1, 3, 4]);
    }

    /// Finding #1 of Etapa 8: with the Core stuck, a queued call answered only after its own deadline plus the
    /// deadline of every call ahead of it (hours with the output polling queued). Each one now answers by its own
    /// deadline, wherever it stands in the queue, and the stream serves at once when the stuck call ends.
    #[test]
    fn a_stuck_call_does_not_add_the_deadlines_of_the_calls_ahead() {
        let lock = Arc::new(FifoLock::new());
        let stuck_for = Duration::from_millis(1500);
        let patience = Duration::from_millis(300);
        // The call the Core does not answer keeps the turn for longer than any deadline below.
        let stuck = {
            let queue = Arc::clone(&lock);
            thread::spawn(move || {
                let _turn = queue.lock();
                thread::sleep(stuck_for);
            })
        };
        wait_for_tickets(&lock, 1);
        let mut polls = Vec::new();
        for poll in 0..3u64 {
            let queue = Arc::clone(&lock);
            polls.push(thread::spawn(move || {
                let start = Instant::now();
                let served = queue.lock_until(start + patience).is_some();
                (served, start.elapsed())
            }));
            wait_for_tickets(&lock, poll + 2);
        }
        let start = Instant::now();
        let operator = lock.lock_until(start + patience).is_some();
        let waited = start.elapsed();
        assert!(!operator && waited < patience + SLACK, "a ação do operador esperou {waited:?}");
        for poll in polls {
            let (served, waited) = poll.join().expect("polling");
            assert!(!served && waited < patience + SLACK, "{waited:?}");
        }
        stuck.join().expect("chamada presa");
        let start = Instant::now();
        assert!(lock.lock_until(start + Duration::from_secs(5)).is_some());
        assert!(start.elapsed() < SLACK);
    }

    #[test]
    fn nothing_is_sent_once_the_deadline_has_passed() {
        let (bridge, requests, _replies) = fake_bridge(Some(live_child()));
        let past = Instant::now();
        thread::sleep(Duration::from_millis(5));
        let failure = bridge.exchange("vault.status", json!({}), past).err().expect("prazo vencido");
        assert!(matches!(failure, IpcFailure::Timeout(30)));
        assert!(requests.try_recv().is_err(), "o pedido não pode chegar ao Core");
    }

    #[test]
    fn a_timeout_frees_the_stream_and_the_late_answer_is_discarded() {
        let (bridge, requests, replies) = fake_bridge(Some(live_child()));
        let core = answer_after(requests, replies, Duration::from_millis(300));
        let start = Instant::now();
        let failure = bridge
            .exchange("terminal.get_output", json!({}), start + Duration::from_millis(100))
            .err()
            .expect("prazo esgotado");
        assert!(matches!(failure, IpcFailure::Timeout(30)));
        assert!(start.elapsed() < Duration::from_millis(100) + SLACK);
        let structured = failure.to_structured();
        assert_eq!(structured["code"], json!(-32007));
        assert_eq!(structured["data"], json!({ "field": "ipc", "reason": "timeout" }));
        // The next call receives the late answer of the first one first and drops it.
        let next = bridge.call_structured("settings.get", json!({})).expect("resposta da segunda chamada");
        assert_eq!(next, json!({ "method": "settings.get" }));
        drop(bridge);
        core.join().expect("Core simulado");
    }

    #[test]
    fn a_core_that_is_down_reports_disconnected_at_the_deadline() {
        let (bridge, _requests, _replies) = fake_bridge(None);
        let failure = bridge
            .exchange("vault.status", json!({}), Instant::now() + Duration::from_millis(50))
            .err()
            .expect("sem Core");
        assert!(matches!(failure, IpcFailure::Disconnected));
        assert_eq!(failure.to_structured()["data"]["reason"], json!("disconnected"));
    }
}
