//! PhotoCraft's Android Storage Access Framework adapter for craft::host::Files.
//! Picker results are polled on the Android UI thread; the Files trait itself remains Send + Sync.

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use craft::host::{FileError, Files, PickId, PickKind, PickRequest, Picked};

use super::{AndroidFileBridge, write_mobile};

#[derive(Clone, Default)]
pub(crate) struct AndroidFiles {
    state: Arc<Mutex<State>>,
}

#[derive(Default)]
struct State {
    next_id: u64,
    pending: HashMap<u64, Pending>,
    answered: HashMap<u64, Picked>,
    last_folders: HashMap<String, PathBuf>,
}

struct Pending {
    request: PickRequest,
    save_bytes: Option<Vec<u8>>,
    started: bool,
}

impl AndroidFiles {
    fn enqueue(&self, request: PickRequest, save_bytes: Option<Vec<u8>>) -> PickId {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        state.next_id = state.next_id.wrapping_add(1).max(1);
        let id = state.next_id;
        state.pending.insert(
            id,
            Pending {
                request,
                save_bytes,
                started: false,
            },
        );
        PickId(id)
    }

    /// Start queued SAF requests and deliver completed results to PhotoCraft's host file service.
    /// This method must run on the Android activity/UI thread because the Java picker belongs to
    /// the Activity. The Files trait methods only enqueue and collect results.
    pub(crate) fn poll(&self, bridge: &AndroidFileBridge, ctx: &egui::Context) {
        let launch = {
            let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            let mut launch = Vec::new();
            for (&id, pending) in state.pending.iter_mut() {
                if !pending.started {
                    pending.started = true;
                    launch.push((id, pending.request.clone()));
                }
            }
            launch
        };

        for (id, request) in launch {
            let result = match request.kind {
                PickKind::Open => bridge.request_open(id, false),
                PickKind::OpenMany => bridge.request_open(id, true),
                PickKind::Folder => bridge.request_folder(id),
                PickKind::Save => {
                    let name = request
                        .file_name
                        .as_deref()
                        .and_then(|name| Path::new(name).file_name())
                        .map(|name| name.to_string_lossy().into_owned())
                        .filter(|name| !name.is_empty())
                        .unwrap_or_else(|| "Untitled.pcraft".to_string());
                    bridge.request_save(id, &name)
                }
            };
            if let Err(error) = result {
                let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
                state.pending.remove(&id);
                state.answered.insert(
                    id,
                    Picked::Unavailable(format!("Could not start Android file picker: {error}")),
                );
                ctx.request_repaint();
            }
        }

        for _ in 0..8 {
            let raw = match bridge.poll_result() {
                Ok(Some(raw)) => raw,
                Ok(None) => break,
                Err(error) => {
                    log::error!("Android file picker polling failed: {error}");
                    break;
                }
            };
            let value: serde_json::Value = match serde_json::from_str(&raw) {
                Ok(value) => value,
                Err(error) => {
                    log::error!("Invalid Android picker result: {error}: {raw}");
                    continue;
                }
            };
            let Some(id) = value.get("id").and_then(serde_json::Value::as_u64) else {
                log::error!("Android picker result has no request id: {raw}");
                continue;
            };

            let Some(pending) = self
                .state
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .pending
                .remove(&id)
            else {
                log::warn!("Ignoring stale Android picker reply {id}");
                continue;
            };

            let status = value
                .get("status")
                .and_then(serde_json::Value::as_str)
                .unwrap_or("error");
            let answer = match status {
                "cancel" => Picked::Cancelled,
                "ok" => match pending.request.kind {
                    PickKind::Open | PickKind::OpenMany => {
                        let paths = value
                            .get("paths")
                            .and_then(serde_json::Value::as_array)
                            .into_iter()
                            .flatten()
                            .filter_map(serde_json::Value::as_str)
                            .filter(|path| !path.is_empty())
                            .map(PathBuf::from)
                            .collect::<Vec<_>>();
                        if paths.is_empty() {
                            Picked::Cancelled
                        } else {
                            Picked::Paths(paths)
                        }
                    }
                    PickKind::Folder | PickKind::Save => {
                        let Some(path) = value
                            .get("path")
                            .and_then(serde_json::Value::as_str)
                            .filter(|path| !path.is_empty())
                        else {
                            let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
                            state.answered.insert(
                                id,
                                Picked::Failed("Android picker returned no file path".to_string()),
                            );
                            ctx.request_repaint();
                            continue;
                        };
                        if let Some(bytes) = pending.save_bytes {
                            match write_mobile(bridge, path, &bytes) {
                                Ok(()) => Picked::Paths(vec![PathBuf::from(path)]),
                                Err(error) => Picked::Failed(error),
                            }
                        } else {
                            Picked::Paths(vec![PathBuf::from(path)])
                        }
                    }
                },
                _ => Picked::Unavailable(
                    value
                        .get("message")
                        .and_then(serde_json::Value::as_str)
                        .unwrap_or("Android file operation failed")
                        .to_owned(),
                ),
            };

            let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            if let Picked::Paths(paths) = &answer {
                if let Some(path) = paths.first() {
                    let folder = if pending.request.kind == PickKind::Folder {
                        path.clone()
                    } else {
                        path.parent().unwrap_or(path).to_path_buf()
                    };
                    state
                        .last_folders
                        .insert(pending.request.purpose.clone(), folder);
                }
            }
            state.answered.insert(id, answer);
            ctx.request_repaint();
        }

        if self.pick_pending() {
            ctx.request_repaint_after(std::time::Duration::from_millis(50));
        }
    }
}

impl Files for AndroidFiles {
    fn pick(&self, request: PickRequest) -> PickId {
        self.enqueue(request, None)
    }

    fn take_answer(&self, id: PickId) -> Option<Picked> {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .answered
            .remove(&id.0)
    }

    fn forget(&self, id: PickId) {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        state.pending.remove(&id.0);
        state.answered.remove(&id.0);
    }

    fn pick_pending(&self) -> bool {
        !self
            .state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .pending
            .is_empty()
    }

    fn read(&self, path: &Path) -> Result<Vec<u8>, FileError> {
        fs::read(path).map_err(|error| match error.kind() {
            std::io::ErrorKind::NotFound => FileError::NotFound(path.to_path_buf()),
            std::io::ErrorKind::PermissionDenied => FileError::PermissionDenied(path.to_path_buf()),
            _ => FileError::Other(error.to_string()),
        })
    }

    fn write(&self, path: &Path, bytes: &[u8]) -> Result<(), FileError> {
        if let Some(parent) = path.parent().filter(|parent| !parent.as_os_str().is_empty()) {
            fs::create_dir_all(parent).map_err(|error| FileError::Other(error.to_string()))?;
        }
        let temporary = path.with_extension(format!(
            "{}.tmp-{}",
            path.extension().and_then(|ext| ext.to_str()).unwrap_or(""),
            std::process::id()
        ));
        fs::write(&temporary, bytes).map_err(|error| FileError::Other(error.to_string()))?;
        if let Err(error) = fs::rename(&temporary, path) {
            let _ = fs::remove_file(&temporary);
            return Err(FileError::Other(error.to_string()));
        }
        Ok(())
    }

    fn last_folder(&self, purpose: &str) -> Option<PathBuf> {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .last_folders
            .get(purpose)
            .cloned()
    }

    fn save(&self, mut request: PickRequest, bytes: Vec<u8>) -> PickId {
        request.kind = PickKind::Save;
        self.enqueue(request, Some(bytes))
    }

    fn can_pick(&self) -> bool {
        true
    }

    fn browser_saves(&self) -> bool {
        false
    }
}
