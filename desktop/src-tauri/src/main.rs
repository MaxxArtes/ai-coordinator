// AI Coordinator Desktop — Native Host (Tauri 2 / Rust)
//
// Sole authority of execution and governance remains with the Core Python runtime.
// The Rust host manages window lifecycle, native PTY bridging, and process tree telemetries.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashSet;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use sysinfo::{Pid, ProcessRefreshKind, System};
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
        // Issue A: Ephemeral CSPRNG token generated via uuid v4
        let token = format!("desktop-token-{}", uuid::Uuid::new_v4());
        let (py_path_str, child_proc, child_in, child_out) = match Self::resolve_paths() {
            Ok((py_path, script_path)) => {
                let mut cmd = Command::new(&py_path);
                cmd.arg("-I")
                    .arg(&script_path)
                    .env("COORDINATOR_IPC_TOKEN", &token)
                    .stdin(Stdio::piped())
                    .stdout(Stdio::piped())
                    .stderr(Stdio::inherit());

                let mut proc = None;
                let mut cin = None;
                let mut cout = None;

                match cmd.spawn() {
                    Ok(mut child) => {
                        if let Some(cin_handle) = child.stdin.take() {
                            cin = Some(cin_handle);
                        }
                        if let Some(cout_handle) = child.stdout.take() {
                            cout = Some(BufReader::new(cout_handle));
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
            stdin: Mutex::new(child_in),
            reader: Mutex::new(child_out),
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
            } else {
                #[cfg(debug_assertions)]
                {
                    if std::env::var("COORDINATOR_STRICT_RUNTIME").is_ok() {
                        return Err(
                            "Fail-closed: Bundled Python runtime not found and COORDINATOR_STRICT_RUNTIME is set.".to_string(),
                        );
                    }
                    eprintln!(
                        "[TAURI_IPC_WARN] Bundled Python runtime not found. Falling back to host PATH 'python' (debug/dev mode only)."
                    );
                    PathBuf::from("python")
                }
                #[cfg(not(debug_assertions))]
                {
                    return Err(
                        "Fail-closed: Private bundled Python runtime not found in application resources, and COORDINATOR_PYTHON not set. Refusing to run in release mode without verified private runtime.".to_string(),
                    );
                }
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
                return Err("IPC child stdin is not available: runtime is not initialized".to_string());
            }
        }

        // 2. Read single line response from child stdout
        let mut resp_line = String::new();
        {
            let mut reader_guard = self.reader.lock().map_err(|e| e.to_string())?;
            if let Some(reader) = reader_guard.as_mut() {
                reader.read_line(&mut resp_line).map_err(|e| e.to_string())?;
            } else {
                return Err("IPC child stdout is not available: runtime is not initialized".to_string());
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

// Issue C: Synchronized process tree termination on window close
impl Drop for IpcBridge {
    fn drop(&mut self) {
        if let Ok(mut guard) = self.child.lock() {
            if let Some(mut child) = guard.take() {
                let pid = child.id();
                #[cfg(target_os = "windows")]
                {
                    // Synchronously kill the entire process tree using taskkill /F /T /PID
                    if let Ok(mut kill_cmd) = Command::new("taskkill")
                        .args(["/F", "/T", "/PID", &pid.to_string()])
                        .stdout(Stdio::null())
                        .stderr(Stdio::null())
                        .spawn()
                    {
                        let _ = kill_cmd.wait();
                    }
                }
                let _ = child.kill();
                let _ = child.wait();
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
    let is_isolated = !bridge.resolved_python.starts_with("UNAVAILABLE");
    RuntimeStatus {
        core_version: "0.6.0".into(),
        host_os: std::env::consts::OS.into(),
        is_isolated,
        python_path: bridge.resolved_python.clone(),
    }
}

// Issue D: Recursively measure all process tree descendants (children, grandchildren, WebView2 renderers)
#[tauri::command]
fn get_process_tree_metrics() -> ProcessTreeMetrics {
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
