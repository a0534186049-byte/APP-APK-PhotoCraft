#!/usr/bin/env python3
"""Apply PhotoCraft-only Android compatibility patches to the pinned source and builder checkout.

This patcher does not copy or modify WordCraft/GridCraft application code. It fixes
PhotoCraft Builder integration, Android gesture hand-off, and the mobile text-action bar.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def fail(message: str) -> "NoReturn":
    raise SystemExit(f"PhotoCraft Android preparation failed: {message}")


def patch_once(path: Path, old: str, new: str, label: str) -> bool:
    if not path.is_file():
        fail(f"required file is missing: {path}")
    text = path.read_text(encoding="utf-8")
    old_count = text.count(old)
    new_count = text.count(new) if new else 0
    if new_count == 1:
        print(f"  Already applied: {label}")
        return False
    if old_count == 1 and new_count == 0:
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        print(f"  Applied: {label}")
        return True
    fail(
        f"{label}: expected exactly one original or patched block "
        f"(original={old_count}, patched={new_count}) in {path}"
    )


def replace_regex_once(path: Path, pattern: str, replacement: str, marker: str, label: str) -> bool:
    if not path.is_file():
        fail(f"required file is missing: {path}")
    text = path.read_text(encoding="utf-8")
    if marker in text:
        print(f"  Already applied: {label}")
        return False
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.DOTALL)
    if count != 1:
        fail(f"{label}: expected exactly one original block in {path}, found {count}")
    path.write_text(updated, encoding="utf-8")
    print(f"  Applied: {label}")
    return True


def main() -> None:
    if len(sys.argv) != 3:
        fail("usage: prepare-photocraft-android.py <source-dir> <builder-root>")

    source = Path(sys.argv[1]).resolve()
    builder = Path(sys.argv[2]).resolve()

    cargo = source / "Cargo.toml"
    i18n_rev = source / "craft-i18n.rev"
    i18n_rs = source / "crates/ui-egui/src/i18n.rs"
    app_lib = source / "crates/ui-egui/src/lib.rs"
    if not cargo.is_file() or not i18n_rev.is_file():
        fail("the pinned PhotoCraft source tree is incomplete")
    rev = i18n_rev.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", rev):
        fail("craft-i18n.rev must contain one full immutable commit SHA")

    i18n_text = i18n_rs.read_text(encoding="utf-8")
    lib_text = app_lib.read_text(encoding="utf-8")
    if not re.search(r'LangInfo\s*\{\s*code:\s*"he",\s*complete_menus:\s*true', i18n_text):
        fail("the pinned source does not register Hebrew as a complete-menu language")
    if "pub mod i18n;" not in lib_text or 'pub fn t(s: &str) -> &str' not in i18n_text:
        fail("the public PhotoCraft translation API needed by the Android bar is missing")

    template = builder / "templates/android/mobile_text_bar.rs"
    if not template.is_file():
        fail(f"the pinned builder has no Android mobile text bar template: {template}")
    # The pinned egui-winit fork destructures every PlatformOutput field explicitly.
    # PhotoCraft pins a newer egui that added retired_ime_resets, so this vendor
    # fork must ignore fields it does not consume to remain source-compatible.
    egui_winit = builder / "vendor/egui-winit/src/lib.rs"
    patch_once(
        egui_winit,
        "            request_discard_reasons: _, // `egui::Context::run` handles this\n        } = platform_output;",
        "            request_discard_reasons: _, // `egui::Context::run` handles this\n"
        "            .. // Ignore newer PlatformOutput fields unused by egui-winit.\n"
        "        } = platform_output;",
        "ignore newer unused egui PlatformOutput fields",
    )

    # Keep Android rotation under the device user's rotation setting, not raw sensor rotation.
    adapter_manifest = builder / "adapters/photocraft/Cargo.toml"
    patch_once(
        adapter_manifest,
        'orientation = "sensor"',
        'orientation = "user"',
        "honor Android user rotation preference",
    )
    # The pinned PhotoCraft workspace moved image codecs into craft-libs. The
    # Builder adapter still points at the removed local crates/codecs directory.
    # Resolve the dependency through the workspace alias defined by PhotoCraft.
    patch_once(
        adapter_manifest,
        'photocraft-codecs = { path = "../../crates/codecs" }',
        'photocraft-codecs = { workspace = true }',
        "use the workspace shared image-codecs dependency",
    )

    # The upstream app moved control_server.rs into craft-libs on 17 November.
    # Keep the last PhotoCraft-owned implementation as an app-specific Android adapter fallback.
    fallback_source = Path(__file__).resolve().parent.parent / "adapters/photocraft/src/control_server.rs"
    staged_control = builder / "adapters/photocraft/src/control_server.rs"
    if not fallback_source.is_file():
        fail(f"PhotoCraft Android control-server compatibility source is missing: {fallback_source}")
    fallback_text = fallback_source.read_text(encoding="utf-8")
    staged_control.parent.mkdir(parents=True, exist_ok=True)
    if staged_control.is_file():
        if staged_control.read_text(encoding="utf-8") != fallback_text:
            fail(f"refusing to overwrite a different builder control-server module: {staged_control}")
        print("  Already staged: PhotoCraft Android control-server compatibility module")
    else:
        staged_control.write_text(fallback_text, encoding="utf-8")
        print("  Staged: PhotoCraft Android control-server compatibility module")

    prepare_script = builder / "adapters/photocraft/prepare.sh"
    old_upstream_control = '''if not upstream.is_file():
    raise SystemExit(
        f"photocraft: upstream no longer has {upstream}; the Android control server has to be "
        "adapted by hand"
    )

text = upstream.read_text()'''
    compatible_control = '''if upstream.is_file():
    text = upstream.read_text()
elif target.is_file():
    text = target.read_text()
    print("photocraft: using the pinned PhotoCraft Android control-server compatibility module")
else:
    raise SystemExit(
        f"photocraft: neither upstream nor the app-specific Android adapter provides {target}"
    )'''
    patch_once(
        prepare_script,
        old_upstream_control,
        compatible_control,
        "fall back to the PhotoCraft-owned Android control-server module after upstream relocation",
    )


    # The current PhotoCraft host uses craft::host::Files, not the retired browser-only
    # FileDialogReply callback. Install the Android SAF-backed implementation for this app.
    app_adapter = builder / "adapters/photocraft/src/lib.rs"
    android_files_source = Path(__file__).resolve().parent.parent / "adapters/photocraft/src/android_files.rs"
    staged_android_files = builder / "adapters/photocraft/src/android_files.rs"
    if not android_files_source.is_file():
        fail(f"PhotoCraft Android Files adapter is missing: {android_files_source}")
    files_text = android_files_source.read_text(encoding="utf-8")
    staged_android_files.parent.mkdir(parents=True, exist_ok=True)
    if staged_android_files.is_file():
        if staged_android_files.read_text(encoding="utf-8") != files_text:
            fail(f"refusing to overwrite a different Android Files adapter: {staged_android_files}")
        print("  Already staged: Android SAF Files service")
    else:
        staged_android_files.write_text(files_text, encoding="utf-8")
        print("  Staged: Android SAF Files service")

    patch_once(
        app_adapter,
        "use photocraft_ui_egui::{\n    FileDialogAnswer, FileDialogReply, FileDialogRequest, Inbox, PhotocraftApp, Recoverable,\n    Services,\n};",
        "use photocraft_ui_egui::{Inbox, PhotocraftApp, Recoverable, Services};\nuse android_files::AndroidFiles;",
        "use the current PhotoCraft host file service API",
    )
    patch_once(
        app_adapter,
        "mod control_server;\n",
        "mod control_server;\nmod android_files;\n",
        "include the PhotoCraft Android SAF Files module",
    )
    patch_once(
        app_adapter,
        "use std::sync::atomic::{AtomicU64, Ordering};",
        "use std::sync::atomic::Ordering;",
        "remove obsolete Android picker counter import",
    )
    replace_regex_once(
        app_adapter,
        r"(?s)#\[derive\(Default\)\]\nstruct PickerState \{.*?\n\}\n\n(?=fn write_mobile)",
        "// Android SAF picker implementation using the current craft::host::Files API.\n\n",
        "Android SAF picker implementation using the current craft::host::Files API",
        "replace the obsolete callback-based picker with the current Files service",
    )
    patch_once(
        app_adapter,
        "fn services(bridge: AndroidFileBridge, picker: Arc<PickerState>, prefs_path: PathBuf, inbox: Inbox) -> Services {",
        "fn services(bridge: AndroidFileBridge, files: AndroidFiles, prefs_path: PathBuf, inbox: Inbox) -> Services {",
        "accept the new Android Files service in PhotoCraft services",
    )
    patch_once(
        app_adapter,
        "    let prefs_bridge = bridge.clone_for_services();\n    let load_prefs_path = prefs_path.clone();\n",
        "",
        "remove the retired JSON preference callbacks",
    )
    patch_once(
        app_adapter,
        "    Services {\n        // Files the system asked us to open",
        "    Services {\n        files: Some(Arc::new(files.clone())),\n        settings: Some(craft::settings::SettingsStore::open_with(\n            photocraft_engine::prefs::store_options(prefs_path.parent()),\n        )),\n        // Files the system asked us to open",
        "connect Android SAF and the current persistent settings store",
    )
    patch_once(
        app_adapter,
        "        file_dialog: Some(Box::new(move |request, _parent, reply| {\n            picker.launch(&bridge, request, reply);\n        })),\n",
        "",
        "remove the obsolete browser-only file dialog callback",
    )
    patch_once(
        app_adapter,
        "        system_theme: Some(Box::new(|_ctx| egui_winit::system_theme::system_theme())),\n",
        "",
        "remove the removed system-theme Services field",
    )
    patch_once(
        app_adapter,
        "        load_prefs: Some(Box::new(move || fs::read_to_string(&load_prefs_path).ok())),\n        save_prefs: Some(Box::new(move |text: &str| {\n            write_mobile(\n                &prefs_bridge,\n                &prefs_path.to_string_lossy(),\n                text.as_bytes(),\n            )\n        })),\n",
        "",
        "use PhotoCraft SettingsStore instead of the removed JSON preference callbacks",
    )
    replace_regex_once(
        app_adapter,
        r"(?s)        encode_png: Some\(Box::new\(\|w, h, rgba\| \{.*?\n        \}\)\),\n",
        "",
        "encode_png: Some",
        "remove the obsolete encode_png service field",
    )
    patch_once(
        app_adapter,
        "    inner: PhotocraftApp,\n    bridge: AndroidFileBridge,\n    picker: Arc<PickerState>,",
        "    inner: Box<PhotocraftApp>,\n    bridge: AndroidFileBridge,\n    files: AndroidFiles,",
        "match PhotoCraft's boxed app and retain the SAF Files service",
    )
    patch_once(
        app_adapter,
        "        let current: Vec<String> = self.inner.ui.recent_files.clone();",
        "        let current: Vec<String> = self.inner.ui.recent_files.to_vec();",
        "read tracked recent file paths through the current API",
    )
    patch_once(
        app_adapter,
        "        self.picker.poll(&self.bridge, ctx);",
        "        self.files.poll(&self.bridge, ctx);",
        "poll Android SAF through the current Files service",
    )
    replace_regex_once(
        app_adapter,
        r"(?s)\n        // Sync appearance when system theme changes if set to Auto\n        if self\.inner\.session\.prefs\(\)\.interface\.appearance_mode == photocraft_engine::prefs::AppearanceMode::Auto \{.*?\n        \}\n        self\.inner\.logic",
        "\n        self.inner.logic",
        "// PhotoCraft derives the active theme from the current AppTheme/preferences API.",
        "remove the obsolete ThemeKind/system-theme adapter block",
    )
    patch_once(
        app_adapter,
        "            let initial_theme = if egui_winit::system_theme::system_theme() == Some(egui::Theme::Light) {\n                photocraft_ui_egui::theme::ThemeKind::ProLight\n            } else {\n                photocraft_ui_egui::theme::ThemeKind::Pro\n            };",
        "            let initial_theme = photocraft_ui_egui::theme::AppTheme::default();",
        "initialize PhotoCraft with its current AppTheme type",
    )
    patch_once(
        app_adapter,
        "    let picker = Arc::new(PickerState::default());",
        "    let files = AndroidFiles::default();",
        "create the Android SAF Files service",
    )
    patch_once(
        app_adapter,
        "    let app_picker = picker.clone();",
        "    let app_files = files.clone();",
        "share the Android SAF Files service with the app closure",
    )
    patch_once(
        app_adapter,
        "                    app_picker.clone(),",
        "                    app_files.clone(),",
        "pass the Android SAF Files service into PhotoCraft",
    )
    patch_once(
        app_adapter,
        "                picker: app_picker.clone(),",
        "                files: app_files.clone(),",
        "store the Android SAF Files service in the app shell",
    )

    # The Android adapter is its own crate; add the workspace facade as a direct dependency.
    adapter_manifest = builder / "adapters/photocraft/Cargo.toml"
    adapter_manifest_text = adapter_manifest.read_text(encoding="utf-8")
    if 'craft = { workspace = true }' not in adapter_manifest_text:
        patch_once(
            adapter_manifest,
            "[dependencies]\n",
            "[dependencies]\ncraft = { workspace = true }\n",
            "add the current craft host/settings API to the Android adapter",
        )

    patched_adapter = app_adapter.read_text(encoding="utf-8")
    for stale in ("FileDialogAnswer", "FileDialogReply", "FileDialogRequest", "PickerState", "ThemeKind", "file_dialog:", "system_theme:", "load_prefs:", "save_prefs:", "encode_png:"):
        if stale in patched_adapter:
            fail(f"the Android adapter still contains a retired PhotoCraft API: {stale}")
    if "files: Some(Arc::new(files.clone()))" not in patched_adapter or "SettingsStore::open_with" not in patched_adapter:
        fail("the current PhotoCraft Files or persistent settings service was not installed")

    # When a second finger takes over a brush press, release the first pointer at its
    # last known position. Otherwise a stroke can remain active during a two-finger pan.
    touch_template = builder / "templates/android/touch_gestures.rs"
    patch_once(
        touch_template,
        "                                cancel_pos = Some(*pos);",
        "                                cancel_pos = state.fingers.values().next().map(|finger| finger.last_pos);",
        "remember the first finger position when scroll or pinch takes over",
    )
    old_multi_touch_filter = '''    if is_multi_touch {
        raw.events.retain(|event| !matches!(event, Event::PointerButton { .. }));
    }'''
    release_pointer_on_takeover = '''    if is_multi_touch {
        raw.events.retain(|event| !matches!(event, Event::PointerButton { .. }));
    }
    if cancel_stroke {
        if let Some(pos) = cancel_pos {
            raw.events.push(Event::PointerButton {
                pos,
                button: egui::PointerButton::Primary,
                pressed: false,
                modifiers: Modifiers::NONE,
            });
        }
    }'''
    patch_once(
        touch_template,
        old_multi_touch_filter,
        release_pointer_on_takeover,
        "release an active PhotoCraft brush stroke when a two-finger gesture takes over",
    )

    original_actions = '''    let actions = [
        ("Cut", Action::Cut),
        ("Copy", Action::Copy),
        ("Paste", Action::Paste),
        ("Delete", Action::Delete),
    ];'''
    translated_actions = '''    // PhotoCraft-only: use the same embedded catalog as the rest of the interface.
    let actions = [
        (photocraft_ui_egui::i18n::t("Cut"), Action::Cut),
        (photocraft_ui_egui::i18n::t("Copy"), Action::Copy),
        (photocraft_ui_egui::i18n::t("Paste"), Action::Paste),
        (photocraft_ui_egui::i18n::t("Delete"), Action::Delete),
    ];'''
    patch_once(template, original_actions, translated_actions, "Hebrew-aware mobile action labels")

    original_size = '''    let btn_w = 70.0f32;
    let btn_h = 30.0f32;
    let pad = 4.0f32;
    let spacing = 4.0f32;
    let total_w = 2.0 * pad + actions.len() as f32 * btn_w + actions.len().saturating_sub(1) as f32 * spacing;
    let total_h = btn_h + 2.0 * pad;

    let screen = ctx.content_rect();'''
    responsive_size = '''    // PhotoCraft-only: keep the action bar inside narrow phone and split-screen windows.
    let pad = 4.0f32;
    let spacing = 4.0f32;
    let screen = ctx.content_rect();
    let btn_w = ((screen.width() - 16.0 - 2.0 * pad
        - actions.len().saturating_sub(1) as f32 * spacing)
        / actions.len() as f32)
        .clamp(28.0, 70.0);
    let btn_h = if screen.height() < 420.0 { 30.0f32 } else { 36.0f32 };
    let total_w = 2.0 * pad + actions.len() as f32 * btn_w + actions.len().saturating_sub(1) as f32 * spacing;
    let total_h = btn_h + 2.0 * pad;'''
    patch_once(template, original_size, responsive_size, "screen-width-aware action bar sizing")

    original_font = "                    FontId::proportional(12.5),"
    responsive_font = "                    FontId::proportional((btn_w * 0.19).clamp(9.0, 12.5)),"
    patch_once(template, original_font, responsive_font, "action label font scaling")

    # Verify the app-specific Android compatibility patches as well as the narrow action bar.
    control_server_source = staged_control.read_text(encoding="utf-8")
    if "pub fn start(listener: TcpListener, token: String, ctx: egui::Context)" not in control_server_source:
        fail("PhotoCraft Android control-server fallback does not expose the expected start API")
    if 'orientation = "user"' not in adapter_manifest.read_text(encoding="utf-8"):
        fail("PhotoCraft Android activity is not configured to honor the user's rotation preference")
    if 'photocraft-codecs = { workspace = true }' not in adapter_manifest.read_text(encoding="utf-8"):
        fail("PhotoCraft Android adapter still points to the removed local codecs crate")
    if "Ignore newer PlatformOutput fields unused by egui-winit." not in egui_winit.read_text(encoding="utf-8"):
        fail("egui-winit PlatformOutput destructuring is not forward-compatible")
    touch_text = touch_template.read_text(encoding="utf-8")
    for marker in ("button: egui::PointerButton::Primary", "pressed: false", "finger.last_pos"):
        if marker not in touch_text:
            fail(f"Android gesture hand-off verification did not find {marker!r}")

    final_text = template.read_text(encoding="utf-8")
    for marker in (
        'photocraft_ui_egui::i18n::t("Cut")',
        'photocraft_ui_egui::i18n::t("Copy")',
        'photocraft_ui_egui::i18n::t("Paste")',
        'photocraft_ui_egui::i18n::t("Delete")',
        "screen.width() - 16.0",
        "btn_w * 0.19",
    ):
        if marker not in final_text:
            fail(f"post-patch verification did not find {marker!r}")

    print(f"PhotoCraft Android patcher OK; Hebrew catalog source is pinned at {rev}.")


if __name__ == "__main__":
    main()
