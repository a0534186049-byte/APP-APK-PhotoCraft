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
    if old_count == 1 and new_count == 0:
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        print(f"  Applied: {label}")
        return True
    if old_count == 0 and new_count == 1:
        print(f"  Already applied: {label}")
        return False
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
