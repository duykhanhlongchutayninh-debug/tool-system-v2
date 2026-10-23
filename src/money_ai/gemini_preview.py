"""Show the REAL last Gemini UI error directly under its Kaggle button.

Read a private saved screenshot first; when missing, attach to the owner's
EXISTING Chrome CDP 9222 and read an EXISTING Gemini tab. Never launch any
browser, open/close a page, click, navigate, send a Telegram image, or write
user screenshots to GitHub/Supabase. Screenshots remain in the Kaggle runtime.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

IMAGE = Path("/kaggle/working/tool-system-v2-runtime/state/ui/gemini_step06_last_error.jpg")
MAX_BYTES = 8_000_000


def _safe_jpeg(data: bytes) -> bool:
    return (100 <= len(data) <= MAX_BYTES and data.startswith(b"\xff\xd8")
            and data.endswith(b"\xff\xd9"))


def _existing_gemini_tab(contexts) -> object | None:
    """Only inspect already-open Gemini pages in the one shared browser."""
    for context in contexts:
        for tab in reversed(context.pages):
            try:
                target=urlparse(tab.url)
                if target.scheme=="https" and (
                        target.hostname or "").lower() == "gemini.google.com":
                    return tab
            except Exception:
                continue
    return None


def obtain_private_preview(
        saved: Path = IMAGE, *, live_capture=None) -> tuple[bytes, str]:
    """Read saved error image; otherwise make a read-only same-Chrome snapshot.

    An injectable live_capture callable supports offline tests and does not
    grant the agent extra control. Browser capture uses Playwright CDP attach,
    and only stops its own connection, not the owner's Chrome process.
    """
    if saved.is_file():
        if saved.stat().st_size > MAX_BYTES:
            raise RuntimeError("GEMINI_PREVIEW_SAVED_IMAGE_TOO_LARGE")
        data=saved.read_bytes()
        if _safe_jpeg(data):
            return data, "SAVED_ERROR_SCREENSHOT"
        raise RuntimeError("GEMINI_PREVIEW_SAVED_IMAGE_INVALID")

    if live_capture is not None:
        data=live_capture()
    else:
        # Kaggle ipywidgets callbacks can execute inside Jupyter's asyncio
        # event loop. Playwright's sync API is forbidden in that event loop.
        # Attach/read the already-open Chrome in a short worker THREAD with
        # its own Playwright session; never launch or close a Chrome window.
        from queue import Queue, Empty
        from threading import Thread
        done=Queue(maxsize=1)
        def _read_existing_browser():
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as playwright:
                    browser=playwright.chromium.connect_over_cdp(
                        "http://127.0.0.1:9222", timeout=12000)
                    if len(browser.contexts)!=1:
                        raise RuntimeError("GEMINI_PREVIEW_CHROME_PROFILE_NOT_UNIQUE")
                    tab=_existing_gemini_tab(browser.contexts)
                    if tab is None:
                        raise RuntimeError("GEMINI_PREVIEW_NO_EXISTING_GEMINI_TAB")
                    picture=tab.screenshot(type="jpeg", quality=60, timeout=12000)
                    # Exiting Playwright disconnects the CDP transport only.
                    # Do not browser.close(), tab.close() or navigate().
                done.put(("ok",picture))
            except Exception as exc:
                code=str(exc)
                if not (code.startswith("GEMINI_PREVIEW_") and
                        code.replace("_","").isalnum() and len(code)<100):
                    code="GEMINI_PREVIEW_CDP_"+type(exc).__name__
                done.put(("error",code))
        worker=Thread(target=_read_existing_browser,daemon=True,
                      name="money-ai-gemini-private-screenshot")
        worker.start()
        worker.join(timeout=28)
        if worker.is_alive():
            raise RuntimeError("GEMINI_PREVIEW_CDP_CAPTURE_TIMEOUT")
        try:
            state,data=done.get_nowait()
        except Empty as exc:
            raise RuntimeError("GEMINI_PREVIEW_CDP_NO_IMAGE_RESULT") from exc
        if state!="ok":
            raise RuntimeError(data)
    if not isinstance(data,bytes) or not _safe_jpeg(data):
        raise RuntimeError("GEMINI_PREVIEW_LIVE_JPEG_INVALID")
    saved.parent.mkdir(parents=True,exist_ok=True)
    saved.parent.chmod(0o700)
    saved.write_bytes(data)
    saved.chmod(0o600)
    return data, "EXISTING_CHROME_READ_ONLY_SCREENSHOT"
