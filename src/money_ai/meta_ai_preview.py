"""Show the REAL current Meta AI UI when Step07 needs owner reconciliation.

Attach read-only to the single owner Chrome via CDP 9222. Never launch a
browser, navigate, click, type, upload, post, or send the screenshot elsewhere.
Screenshots stay in the Kaggle runtime and are displayed only in the notebook.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

IMAGE = Path("/kaggle/working/tool-system-v2-runtime/state/ui/meta_step07_last_error.jpg")
MAX_BYTES = 8_000_000
META_HOSTS = frozenset({"meta.ai", "www.meta.ai"})


def _safe_jpeg(data: bytes) -> bool:
    return (100 <= len(data) <= MAX_BYTES and data.startswith(b"\xff\xd8")
            and data.endswith(b"\xff\xd9"))


def _existing_meta_tab(contexts):
    """Return only an already-open Meta tab; never create or navigate one."""
    for context in contexts:
        try:
            tabs = list(context.pages)
        except Exception:
            continue
        for tab in reversed(tabs):
            try:
                target = urlparse(tab.url)
                if target.scheme == "https" and (target.hostname or "").lower() in META_HOSTS:
                    return tab
            except Exception:
                continue
    return None


def obtain_private_preview(saved: Path = IMAGE, *, live_capture=None) -> tuple[bytes, str]:
    """Read a saved error screenshot or capture the existing Meta tab read-only."""
    if saved.is_file():
        if saved.stat().st_size > MAX_BYTES:
            raise RuntimeError("METAAI_PREVIEW_SAVED_IMAGE_TOO_LARGE")
        data = saved.read_bytes()
        if _safe_jpeg(data):
            return data, "SAVED_META_ERROR_SCREENSHOT"
        raise RuntimeError("METAAI_PREVIEW_SAVED_IMAGE_INVALID"

    )
    if live_capture is not None:
        data = live_capture()
    else:
        # Kaggle widget callbacks can execute inside Jupyter's asyncio loop.
        # Use an isolated worker thread for Playwright's sync API. CDP disconnect
        # on context exit leaves the existing browser and tabs untouched.
        from queue import Empty, Queue
        from threading import Thread
        done = Queue(maxsize=1)

        def _read_existing_browser():
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as playwright:
                    browser = playwright.chromium.connect_over_cdp(
                        "http://127.0.0.1:9222", timeout=12000)
                    if len(browser.contexts) != 1:
                        raise RuntimeError("METAAI_PREVIEW_CHROME_PROFILE_NOT_UNIQUE")
                    tab = _existing_meta_tab(browser.contexts)
                    if tab is None:
                        raise RuntimeError("METAAI_PREVIEW_NO_EXISTING_META_TAB")
                    picture = tab.screenshot(type="jpeg", quality=60, timeout=12000)
                done.put(("ok", picture))
            except Exception as exc:
                code = str(exc)
                if not (code.startswith("METAAI_PREVIEW_") and
                        code.replace("_", "").isalnum() and len(code) < 100):
                    code = "METAAI_PREVIEW_CDP_" + type(exc).__name__
                done.put(("error", code))

        worker = Thread(target=_read_existing_browser, daemon=True,
                        name="money-ai-meta-private-screenshot")
        worker.start()
        worker.join(timeout=28)
        if worker.is_alive():
            raise RuntimeError("METAAI_PREVIEW_CDP_CAPTURE_TIMEOUT")
        try:
            state, data = done.get_nowait()
        except Empty as exc:
            raise RuntimeError("METAAI_PREVIEW_CDP_NO_IMAGE_RESULT") from exc
        if state != "ok":
            raise RuntimeError(data)
    if not isinstance(data, bytes) or not _safe_jpeg(data):
        raise RuntimeError("METAAI_PREVIEW_LIVE_JPEG_INVALID")
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.parent.chmod(0o700)
    saved.write_bytes(data)
    saved.chmod(0o600)
    return data, "EXISTING_CHROME_READ_ONLY_SCREENSHOT"
