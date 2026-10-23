"""Fix noVNC WebSocket URL when Kaggle serves it under an authenticated path prefix.

Run once inside the existing Kaggle Notebook. Does NOT start any new
browser, desktop, server, process or public tunnel. Never logs credentials.
A local backup of vnc.html is kept for rollback.
"""
from pathlib import Path
import re

ROOT = next(
    (p for p in (Path("/usr/share/novnc"), Path("/usr/local/share/novnc"))
     if (p / "vnc.html").is_file()),
    Path("/usr/share/novnc"),
)
PAGE = ROOT / "vnc.html"
PATCH = ROOT / "app" / "tool_system_proxy_path.js"
BACKUP = ROOT / "vnc.html.money-ai-original"
GESTURE = ROOT / "core" / "input" / "gesturehandler.js"
GESTURE_BACKUP = ROOT / "core" / "input" / "gesturehandler.js.money-ai-original"
UI_FILE = ROOT / "app" / "ui.js"
UI_BACKUP = ROOT / "app" / "ui.js.money-ai-original"
UI_GUARD = "TOOL_SYSTEM_MOBILE_PAN_MODE"
GESTURE_GUARD = "TOOL_SYSTEM_IGNORE_UNTRACKED_TOUCH_END"
TOUCH_END_UNGUARDED = """            let touch = this._tracked.find(t => t.id === id);
            touch.active = false;"""
TOUCH_END_RE = re.compile(
    r"(?P<indent>[ \t]*)let\s+touch\s*=\s*this\._tracked\.find\("
    r"\s*t\s*=>\s*t\.id\s*===\s*id\s*\);"
    r"\s*touch\.active\s*=\s*false;"
)
LEGACY_MARKER = '<script src="app/tool_system_proxy_path.js"></script>'
MARKER = '<script src="app/tool_system_proxy_path.js?v=3"></script>'
PROXY_SCRIPT_RE = re.compile(r'<script src="app/tool_system_proxy_path\.js(?:\?v=\d+)?"></script>')

JS = r"""// Tool System V2: correct noVNC WebSocket URL for Kaggle's authenticated proxy.
// Always replace stale ?path=... from a previous Kaggle session/tab.
// Older noVNC builds form wss://HOST/ + path (ignoring the HTML base path).
(function () {
  const url = new URL(window.location.href);
  const directory = url.pathname.substring(0, url.pathname.lastIndexOf('/') + 1);
  const wanted = directory.replace(new RegExp('^/+'), '') + 'websockify';
  if (url.searchParams.get('path') !== wanted) {
    url.searchParams.set('path', wanted);
    window.history.replaceState(null, '', url.pathname + url.search + url.hash);
  }
})();
"""

def _patch_gesture_handler() -> None:
    """Guard stale touchend events that otherwise crash the mobile noVNC UI.

    Only patch the exact known upstream statement. Preserve one byte-for-byte
    rollback copy. Unknown noVNC versions fail closed rather than being edited
    speculatively.
    """
    if not GESTURE.is_file():
        raise RuntimeError("NOVNC_GESTURE_HANDLER_NOT_FOUND")
    source = GESTURE.read_text(encoding="utf-8")
    if GESTURE_GUARD in source:
        return
    match = TOUCH_END_RE.search(source)
    if match is None:
        raise RuntimeError("NOVNC_GESTURE_HANDLER_LAYOUT_UNKNOWN")
    if not GESTURE_BACKUP.exists():
        GESTURE_BACKUP.write_text(source, encoding="utf-8")
    indent = match.group("indent")
    replacement = "\n".join((
        indent + "let touch = this._tracked.find(t => t.id === id);",
        indent + "// MOBILE SAFETY: stale touchend can arrive after gesture state reset.",
        indent + "// Ignore the untracked touch instead of crashing the noVNC UI.",
        indent + "if (touch === undefined) {",
        indent + "    return; // TOOL_SYSTEM_IGNORE_UNTRACKED_TOUCH_END",
        indent + "}",
        indent + "touch.active = false;",
    ))
    patched = source[:match.start()] + replacement + source[match.end():]
    if patched.count(GESTURE_GUARD) != 1:
        raise RuntimeError("NOVNC_GESTURE_HANDLER_PATCH_INVALID")
    GESTURE.write_text(patched, encoding="utf-8")
    if GESTURE_GUARD not in GESTURE.read_text(encoding="utf-8"):
        raise RuntimeError("NOVNC_GESTURE_HANDLER_PATCH_VERIFY_FAILED")


def _patch_mobile_viewport() -> None:
    """Enable touch panning without calling helpers absent from Kaggle's noVNC bundle."""
    if not UI_FILE.is_file():
        raise RuntimeError("NOVNC_UI_JS_NOT_FOUND")
    source = UI_FILE.read_text(encoding="utf-8")

    # noVNC v1.3 bundles may not export isTouchDevice() into ui.js scope.
    # Use browser-standard feature detection inline, without a function dependency.
    touch_expr = "((navigator.maxTouchPoints || 0) > 0 || ('ontouchstart' in window))"
    mobile_scale_line = (
        "UI.rfb.scaleViewport = !" + touch_expr
        + " && UI.getSetting('resize') === 'scale';"
    )
    marked_scale_line = mobile_scale_line + " // TOOL_SYSTEM_MOBILE_PAN_MODE"
    scaling_line = (
        "const scaling = UI.getSetting('resize') === 'scale' && !" + touch_expr + ";"
    )
    view_anchor = """        // Changing the viewport may change the state of
        // the dragging button
        UI.updateViewDrag();
    },

/* ------^-------
 * /VIEW CLIPPING"""
    replacement_view = """        // Tool System V2: on touch screens, keep the desktop at native resolution
        // and allow panning instead of calling a noVNC-specific touch helper.
        if ((navigator.maxTouchPoints || 0) > 0 || ('ontouchstart' in window)) {
            UI.rfb.clipViewport = true;
            UI.rfb.dragViewport = true;
        }
        // Changing the viewport may change the state of
        // the dragging button
        UI.updateViewDrag();
    },

/* ------^-------
 * /VIEW CLIPPING"""

    fully_patched = (
        source.count(UI_GUARD) == 1
        and source.count(marked_scale_line) == 1
        and scaling_line in source
        and "if ((navigator.maxTouchPoints || 0) > 0 || ('ontouchstart' in window))" in source
        and "UI.rfb.clipViewport = true;" in source
        and "UI.rfb.dragViewport = true;" in source
        and "!isTouchDevice()" not in source
        and "if (isTouchDevice())" not in source
    )
    if fully_patched:
        return

    if UI_GUARD in source:
        # First prefer the clean pre-patch backup. Older releases could omit it,
        # so also reverse our exact known bad injection in-place. Do not guess
        # edits for an unknown UI layout.
        recovered = None
        if UI_BACKUP.is_file():
            backup_source = UI_BACKUP.read_text(encoding="utf-8")
            if UI_GUARD not in backup_source:
                recovered = backup_source

        if recovered is None:
            bad_scale_patterns = (
                re.compile(
                    r"UI\.rfb\.scaleViewport\s*=\s*!\s*\(?\s*isTouchDevice\(\)\s*\)?"
                    r"\s*&&\s*UI\.getSetting\('resize'\)\s*===\s*'scale';"
                    r"\s*//\s*TOOL_SYSTEM_MOBILE_PAN_MODE"
                ),
                re.compile(
                    r"UI\.rfb\.scaleViewport\s*=\s*!\s*\(?\s*isTouchDevice\(\)\s*\)?"
                    r"\s*&&\s*UI\.getSetting\('resize'\)\s*===\s*'scale';"
                ),
            )
            for pattern in bad_scale_patterns:
                source, count = pattern.subn(
                    "UI.rfb.scaleViewport = UI.getSetting('resize') === 'scale';",
                    source,
                )
                if count:
                    break

            source = source.replace(
                "const scaling = UI.getSetting('resize') === 'scale' && !isTouchDevice();",
                "const scaling = UI.getSetting('resize') === 'scale';",
            )
            old_view = """        // Tool System V2: mobile view pans the desktop at native resolution.
        if (isTouchDevice()) {
            UI.rfb.clipViewport = true;
            UI.rfb.dragViewport = true;
        }
        // Changing the viewport may change the state of
        // the dragging button
        UI.updateViewDrag();
    },

/* ------^-------
 * /VIEW CLIPPING"""
            if old_view in source:
                source = source.replace(old_view, view_anchor, 1)

            # Also recognize the newer block variant and any stale marker that
            # remained after a partial prior write.
            source = source.replace(marked_scale_line, scale_line := "UI.rfb.scaleViewport = UI.getSetting('resize') === 'scale';")
            source = source.replace(" // TOOL_SYSTEM_MOBILE_PAN_MODE", "")
            if UI_GUARD in source:
                raise RuntimeError("NOVNC_UI_PARTIAL_PATCH_LAYOUT_UNKNOWN")
            recovered = source

        source = recovered
        if UI_GUARD in source:
            raise RuntimeError("NOVNC_UI_BACKUP_ALREADY_PATCHED")

    # Keep the clean source for rollback after legacy repair, before this patch is applied.
    clean_source = source
    scale_line = "UI.rfb.scaleViewport = UI.getSetting('resize') === 'scale';"
    scaling_anchor = "const scaling = UI.getSetting('resize') === 'scale';"
    if source.count(scale_line) < 1:
        raise RuntimeError("NOVNC_UI_SCALE_LAYOUT_UNKNOWN")
    if source.count(scaling_anchor) != 1 or source.count(view_anchor) != 1:
        raise RuntimeError("NOVNC_UI_VIEWPORT_LAYOUT_UNKNOWN")

    # Change assignments to avoid shrinking the view on touch devices.
    source = source.replace(scale_line, mobile_scale_line)
    source = source.replace(
        mobile_scale_line, marked_scale_line, 1
    )
    source = source.replace(scaling_anchor, scaling_line, 1)
    source = source.replace(view_anchor, replacement_view, 1)

    if source.count(UI_GUARD) != 1:
        raise RuntimeError("NOVNC_UI_VIEWPORT_PATCH_INVALID")
    if "!isTouchDevice()" in source or "if (isTouchDevice())" in source:
        raise RuntimeError("NOVNC_UI_TOUCH_HELPER_MUST_NOT_BE_CALLED")
    if not UI_BACKUP.exists():
        UI_BACKUP.write_text(clean_source, encoding="utf-8")
    UI_FILE.write_text(source, encoding="utf-8")
    patched = UI_FILE.read_text(encoding="utf-8")
    if (UI_GUARD not in patched or patched.count(UI_GUARD) != 1
            or "!isTouchDevice()" in patched or "if (isTouchDevice())" in patched):
        raise RuntimeError("NOVNC_UI_VIEWPORT_PATCH_VERIFY_FAILED")


def main() -> None:
    if not PAGE.is_file():
        raise RuntimeError("NOVNC_NOT_INSTALLED")
    if not UI_FILE.is_file():
        raise RuntimeError("NOVNC_UI_JS_NOT_FOUND")

    # Preflight UI layout before modifying installed noVNC assets.
    ui_source = UI_FILE.read_text(encoding="utf-8")
    scale_line = "UI.rfb.scaleViewport = UI.getSetting('resize') === 'scale';"
    scaling_anchor = "const scaling = UI.getSetting('resize') === 'scale';"
    view_anchor = """        // Changing the viewport may change the state of
        // the dragging button
        UI.updateViewDrag();
    },

/* ------^-------
 * /VIEW CLIPPING"""
    if UI_GUARD not in ui_source and (
        ui_source.count(scale_line) < 1
        or ui_source.count(scaling_anchor) != 1
        or ui_source.count(view_anchor) != 1
    ):
        raise RuntimeError("NOVNC_UI_VIEWPORT_LAYOUT_UNKNOWN")

    original = PAGE.read_text(encoding="utf-8")
    if not BACKUP.exists():
        BACKUP.write_text(original, encoding="utf-8")
    PATCH.parent.mkdir(parents=True, exist_ok=True)
    PATCH.write_text(JS, encoding="utf-8")

    # Insert or upgrade the authenticated WebSocket path helper.
    patched = original
    if MARKER not in original:
        match = PROXY_SCRIPT_RE.search(original)
        if match:
            patched = PROXY_SCRIPT_RE.sub(MARKER, original, count=1)
        elif "<head>" in original:
            patched = original.replace("<head>", "<head>\\n    " + MARKER, 1)
        else:
            raise RuntimeError("NOVNC_HEAD_NOT_FOUND: do not patch unknown HTML")

    # Always cache-bust ui.js, even if the proxy helper marker already exists.
    # Otherwise Android can keep the broken v3 JavaScript cached and show
    # "isTouchDevice is not a function" after the server file has been repaired.
    patched, ui_script_count = re.subn(
        r'(<script[^>]*src=["\'])app/ui\.js(?:\?v=\d+)?(["\'][^>]*></script>)',
        lambda m: m.group(1) + "app/ui.js?v=4" + m.group(2),
        patched, count=1,
    )
    if ui_script_count != 1:
        raise RuntimeError("NOVNC_UI_SCRIPT_TAG_NOT_FOUND")
    if patched != original:
        PAGE.write_text(patched, encoding="utf-8")

    current_page = PAGE.read_text(encoding="utf-8")
    assert MARKER in current_page
    assert current_page.count("tool_system_proxy_path.js") == 1
    _patch_gesture_handler()
    _patch_mobile_viewport()
    print("NOVNC_PROXY_PATH_FIX_READY")
    print("NOVNC_GESTURE_HANDLER_FIX_READY")
    print("NOVNC_MOBILE_PAN_MODE=READY")
    print("REFRESH_EXISTING_NOVNC_TAB; DO_NOT_OPEN_ANOTHER_CHROME")

if __name__ == "__main__":
    main()
