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
use std::sync::{Arc, Mutex};
use sysinfo::{Pid, ProcessRefreshKind, System};
use tauri::{Emitter, State};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

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
    io_lock: Mutex<()>,
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
        let (py_path_str, child_proc, child_in, child_out) = match Self::resolve_paths() {
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
                            cin = Some(cin_handle);
                        }
                        if let Some(cout_handle) = child.stdout.take() {
                            cout = Some(BufReader::new(cout_handle));
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
            stdin: Mutex::new(child_in),
            reader: Mutex::new(child_out),
            io_lock: Mutex::new(()),
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

    fn call_locked(&self, method: &str, params: Value) -> Result<Value, String> {
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

    pub fn call(&self, method: &str, params: Value) -> Result<Value, String> {
        let _io_guard = self
            .io_lock
            .lock()
            .map_err(|e| format!("IPC I/O mutex poisoned: {}", e))?;

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

        self.call_locked(method, params)
    }

    fn acquire_update_freeze(&self) -> (UpdateFreezeGuard<'_>, SessionInspection) {
        let _io_guard = match self.io_lock.lock() {
            Ok(g) => g,
            Err(e) => {
                return (
                    UpdateFreezeGuard {
                        bridge: self,
                        committed: true,
                    },
                    SessionInspection::Unknown(format!("IPC I/O mutex poisoned: {}", e)),
                );
            }
        };

        self.update_frozen.store(true, Ordering::SeqCst);

        match self.call_locked("system.freeze_for_update", json!({})) {
            Ok(val) => {
                if val.get("frozen").and_then(|f| f.as_bool()) != Some(true) {
                    self.update_frozen.store(false, Ordering::SeqCst);
                    let _ = self.call_locked("system.unfreeze_update", json!({}));
                    return (
                        UpdateFreezeGuard {
                            bridge: self,
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
                            bridge: self,
                            committed: true,
                        },
                        insp,
                    );
                }

                (
                    UpdateFreezeGuard {
                        bridge: self,
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
                        bridge: self,
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
        // Step 1: Drop stdin handle to send EOF to the Python IPC server
        if let Ok(mut stdin_guard) = self.stdin.lock() {
            let _ = stdin_guard.take();
        }

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

struct UpdateFreezeGuard<'a> {
    bridge: &'a IpcBridge,
    committed: bool,
}

impl<'a> UpdateFreezeGuard<'a> {
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

impl<'a> Drop for UpdateFreezeGuard<'a> {
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
fn profile_get_active(bridge: State<Arc<IpcBridge>>) -> Result<Value, String> {
    bridge.call("profile.get_active", json!({}))
}

#[tauri::command]
fn profile_authorize_project(
    project_path: String,
    bridge: State<Arc<IpcBridge>>,
) -> Result<Value, String> {
    bridge.call(
        "profile.authorize_project",
        json!({
            "project_path": project_path
        }),
    )
}

#[tauri::command]
fn profile_list_authorized_projects(bridge: State<Arc<IpcBridge>>) -> Result<Value, String> {
    bridge.call("profile.list_authorized_projects", json!({}))
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
    since_seq: Option<u64>,
    bridge: State<Arc<IpcBridge>>,
) -> Result<Value, String> {
    bridge.call(
        "terminal.get_output",
        json!({
            "session_id": session_id,
            "since_seq": since_seq.unwrap_or(0)
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

    // Check active sessions count from Core IPC (fail-closed if check fails)
    let (sessions_known, active_sessions_count, sessions_error) = match count_active_sessions(&bridge) {
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

    // ETAPA 1 (Fail-Closed Pré-Download): Verificar se sessões estão ativas ou se inspeção falhou
    let pre_insp = count_active_sessions(&bridge);
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
        let (freeze_guard, post_insp) = bridge.acquire_update_freeze();
        if let InstallGate::Block(reason) = evaluate_gate(post_insp, force_update, expected_active_sessions) {
            let mut message = format!("Revalidação pré-instalação bloqueada: {}", reason);
            if let Err(e) = freeze_guard.release() {
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

fn main() {
    let bridge = Arc::new(IpcBridge::new());
    let bridge_window = bridge.clone();

    tauri::Builder::default()
        .manage(bridge)
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
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
            terminal_write,
            terminal_resize,
            terminal_get_output,
            terminal_close_session,
            terminal_save_sessions,
            terminal_list_tabs,
            updater_get_channel_info,
            updater_check,
            updater_download_and_install,
            updater_restart_app
        ])
        .run(tauri::generate_context!())
        .expect("error while running AI Coordinator Desktop");
}
