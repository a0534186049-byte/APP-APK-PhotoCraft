#!/usr/bin/env python3
"""Apply PhotoCraft-only Android polish to the pinned source and builder checkout.

This patcher deliberately does not copy or modify WordCraft/GridCraft application code.
It edits only the pinned PhotoCraft build's Android text-action bar template.
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
    new_count = text.count(new)
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

    # Keep Android rotation under the device user's rotation setting, not raw sensor rotation.
    adapter_manifest = builder / "adapters/photocraft/Cargo.toml"
    patch_once(
        adapter_manifest,
        'orientation = "sensor"',
        'orientation = "user"',
        "honor Android user rotation preference",
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
