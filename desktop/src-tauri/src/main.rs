// AI Coordinator Desktop — Native Host (Tauri 2 / Rust)
//
// Sole authority of execution and governance remains with the Core Python runtime.
// The Rust host manages window lifecycle, native PTY bridging, and process tree telemetries.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::{Deserialize, Serialize};
use std::process::Command;
use sysinfo::{Pid, ProcessRefreshKind, ProcessesToUpdate, System};

#[derive(Serialize, Deserialize)]
pub struct RuntimeStatus {
    pub core_version: String,
    pub host_os: String,
    pub is_isolated: bool,
    pub python_path: String,
}

#[derive(Serialize, Deserialize)]
pub struct ProcessTreeMetrics {
    pub host_pid: u32,
    pub host_ram_bytes: u64,
    pub total_tree_ram_bytes: u64,
    pub cpu_usage_percent: f32,
}

#[tauri::command]
fn ping() -> &'static str {
    "pong"
}

#[tauri::command]
fn get_runtime_status() -> RuntimeStatus {
    RuntimeStatus {
        core_version: "0.6.0".into(),
        host_os: std::env::consts::OS.into(),
        is_isolated: true,
        python_path: "desktop/runtime/python_spec.json".into(),
    }
}

#[tauri::command]
fn get_process_tree_metrics() -> ProcessTreeMetrics {
    let mut sys = System::new();
    let current_pid = Pid::from_u32(std::process::id());
    
    // Refresh current process and its children
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

    // Accumulate child processes
    for (pid, proc) in sys.processes() {
        if let Some(parent) = proc.parent() {
            if parent == current_pid {
                total_ram += proc.memory();
            }
        }
    }

    ProcessTreeMetrics {
        host_pid: std::process::id(),
        host_ram_bytes: total_ram,
        total_tree_ram_bytes: total_ram,
        cpu_usage_percent: cpu,
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![
            ping,
            get_runtime_status,
            get_process_tree_metrics
        ])
        .run(tauri::generate_context!())
        .expect("error while running AI Coordinator Desktop");
}
