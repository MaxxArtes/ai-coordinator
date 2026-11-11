// AI Coordinator Desktop — Native Host (Tauri 2 / Rust)
//
// Sole authority of execution and governance remains with the Core Python runtime.
// The Rust host manages window lifecycle, native PTY bridging, and process tree telemetries.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use sysinfo::{Pid, ProcessRefreshKind, ProcessesToUpdate, System};
use tauri::State;

#[derive(Serialize, Deserialize, Clone)]
pub struct RuntimeStatus {
    pub core_version: String,
    pub host_os: String,
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

pub struct IpcBridge {
    child: Mutex<Option<Child>>,
    stdin: Mutex<Option<ChildStdin>>,
    reader: Mutex<Option<BufReader<ChildStdout>>>,
    token: String,
    seq: AtomicU64,
    resolved_python: String,
}

impl IpcBridge {
    pub fn new() -> Self {
        let token = format!("desktop-token-{}", std::process::id());
        let (py_path, script_path) = Self::resolve_paths();

        let mut child_proc = None;
        let mut child_in = None;
        let mut child_out = None;

        let mut cmd = Command::new(&py_path);
        cmd.arg("-I")
            .arg(&script_path)
            .env("COORDINATOR_IPC_TOKEN", &token)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit());

        match cmd.spawn() {
            Ok(mut child) => {
                if let Some(cin) = child.stdin.take() {
                    child_in = Some(cin);
                }
                if let Some(cout) = child.stdout.take() {
                    child_out = Some(BufReader::new(cout));
                }
                child_proc = Some(child);
            }
            Err(e) => {
                eprintln!("[TAURI_IPC_WARN] Failed to spawn Python IPC server: {}", e);
            }
        }

        IpcBridge {
            child: Mutex::new(child_proc),
            stdin: Mutex::new(child_in),
            reader: Mutex::new(child_out),
            token,
            seq: AtomicU64::new(1),
            resolved_python: py_path.to_string_lossy().to_string(),
        }
    }

    fn resolve_paths() -> (PathBuf, PathBuf) {
        let py_path = if let Ok(custom) = std::env::var("COORDINATOR_PYTHON") {
            PathBuf::from(custom)
        } else if let Ok(exe) = std::env::current_exe() {
            let bundled = exe
                .parent()
                .unwrap_or(&PathBuf::from("."))
                .join("runtime")
                .join("python")
                .join("python.exe");
            if bundled.exists() {
                bundled
            } else {
                PathBuf::from("python")
            }
        } else {
            PathBuf::from("python")
        };

        let script_path = if let Ok(custom_script) = std::env::var("COORDINATOR_SERVER_SCRIPT") {
            PathBuf::from(custom_script)
        } else {
            let candidates = [
                PathBuf::from("desktop/ipc/server.py"),
                PathBuf::from("../desktop/ipc/server.py"),
                PathBuf::from("server.py"),
            ];
            candidates
                .into_iter()
                .find(|p| p.exists())
                .unwrap_or_else(|| PathBuf::from("desktop/ipc/server.py"))
        };

        (py_path, script_path)
    }

    pub fn call(&self, method: &str, params: Value) -> Result<Value, String> {
        let id = self.seq.fetch_add(1, Ordering::SeqCst);
        let req = json!({
            "jsonrpc": "2.0",
            "id": id.to_string(),
            "method": method,
            "params": params,
            "session_token": &self.token
        });

        let line = serde_json::to_string(&req).map_err(|e| e.to_string())?;

        // 1. Write request line to child stdin
        {
            let mut stdin_guard = self.stdin.lock().map_err(|e| e.to_string())?;
            if let Some(stdin) = stdin_guard.as_mut() {
                stdin.write_all(line.as_bytes()).map_err(|e| e.to_string())?;
                stdin.write_all(b"\n").map_err(|e| e.to_string())?;
                stdin.flush().map_err(|e| e.to_string())?;
            } else {
                return Err("IPC child stdin is not available".to_string());
            }
        }

        // 2. Read single line response from child stdout
        let mut resp_line = String::new();
        {
            let mut reader_guard = self.reader.lock().map_err(|e| e.to_string())?;
            if let Some(reader) = reader_guard.as_mut() {
                reader.read_line(&mut resp_line).map_err(|e| e.to_string())?;
            } else {
                return Err("IPC child stdout is not available".to_string());
            }
        }

        if resp_line.trim().is_empty() {
            return Err("Empty response received from Core IPC server".to_string());
        }

        let resp: Value = serde_json::from_str(&resp_line).map_err(|e| e.to_string())?;
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
}

impl Drop for IpcBridge {
    fn drop(&mut self) {
        if let Ok(mut guard) = self.child.lock() {
            if let Some(mut child) = guard.take() {
                let _ = child.kill();
            }
        }
    }
}

#[tauri::command]
fn ping() -> &'static str {
    "pong"
}

#[tauri::command]
fn get_runtime_status(bridge: State<Arc<IpcBridge>>) -> RuntimeStatus {
    RuntimeStatus {
        core_version: "0.6.0".into(),
        host_os: std::env::consts::OS.into(),
        is_isolated: true,
        python_path: bridge.resolved_python.clone(),
    }
}

#[tauri::command]
fn get_process_tree_metrics() -> ProcessTreeMetrics {
    let mut sys = System::new();
    let current_pid = Pid::from_u32(std::process::id());

    sys.refresh_processes_specifics(
        ProcessesToUpdate::All,
        true,
        ProcessRefreshKind::everything(),
    );

    let mut total_ram: u64 = 0;
    let mut host_ram: u64 = 0;
    let mut cpu: f32 = 0.0;

    if let Some(proc) = sys.process(current_pid) {
        host_ram = proc.memory();
        total_ram += host_ram;
        cpu = proc.cpu_usage();
    }

    for (_pid, proc) in sys.processes() {
        if let Some(parent) = proc.parent() {
            if parent == current_pid {
                total_ram += proc.memory();
            }
        }
    }

    ProcessTreeMetrics {
        host_pid: std::process::id(),
        host_ram_bytes: host_ram,
        total_tree_ram_bytes: total_ram,
        cpu_usage_percent: cpu,
    }
}

#[tauri::command]
fn terminal_create_tab(
    tab_id: String,
    project_path: String,
    bridge: State<Arc<IpcBridge>>,
) -> Result<Value, String> {
    bridge.call(
        "terminal.create_tab",
        json!({
            "tab_id": tab_id,
            "project_path": project_path
        }),
    )
}

#[tauri::command]
fn terminal_create_session(
    tab_id: String,
    pane_id: u32,
    kind: String,
    command: Option<String>,
    cwd: Option<String>,
    cols: Option<u16>,
    rows: Option<u16>,
    bridge: State<Arc<IpcBridge>>,
) -> Result<Value, String> {
    bridge.call(
        "terminal.create_session",
        json!({
            "tab_id": tab_id,
            "pane_id": pane_id,
            "kind": kind,
            "command": command,
            "cwd": cwd,
            "cols": cols.unwrap_or(80),
            "rows": rows.unwrap_or(24)
        }),
    )
}

#[tauri::command]
fn terminal_write(
    session_id: String,
    data: String,
    bridge: State<Arc<IpcBridge>>,
) -> Result<Value, String> {
    bridge.call(
        "terminal.write",
        json!({
            "session_id": session_id,
            "data": data
        }),
    )
}

#[tauri::command]
fn terminal_resize(
    session_id: String,
    cols: u16,
    rows: u16,
    bridge: State<Arc<IpcBridge>>,
) -> Result<Value, String> {
    bridge.call(
        "terminal.resize",
        json!({
            "session_id": session_id,
            "cols": cols,
            "rows": rows
        }),
    )
}

#[tauri::command]
fn terminal_get_output(
    session_id: String,
    bridge: State<Arc<IpcBridge>>,
) -> Result<Value, String> {
    bridge.call(
        "terminal.get_output",
        json!({
            "session_id": session_id
        }),
    )
}

#[tauri::command]
fn terminal_close_session(
    session_id: String,
    bridge: State<Arc<IpcBridge>>,
) -> Result<Value, String> {
    bridge.call(
        "terminal.close_session",
        json!({
            "session_id": session_id
        }),
    )
}

#[tauri::command]
fn terminal_save_sessions(
    tab_id: String,
    bridge: State<Arc<IpcBridge>>,
) -> Result<Value, String> {
    bridge.call(
        "terminal.save_sessions",
        json!({
            "tab_id": tab_id
        }),
    )
}

#[tauri::command]
fn terminal_list_tabs(bridge: State<Arc<IpcBridge>>) -> Result<Value, String> {
    bridge.call("terminal.list_tabs", json!({}))
}

fn main() {
    let bridge = Arc::new(IpcBridge::new());

    tauri::Builder::default()
        .manage(bridge)
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![
            ping,
            get_runtime_status,
            get_process_tree_metrics,
            terminal_create_tab,
            terminal_create_session,
            terminal_write,
            terminal_resize,
            terminal_get_output,
            terminal_close_session,
            terminal_save_sessions,
            terminal_list_tabs
        ])
        .run(tauri::generate_context!())
        .expect("error while running AI Coordinator Desktop");
}
