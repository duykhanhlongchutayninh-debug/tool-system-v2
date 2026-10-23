"""Tool System V2 step 05: inspect the owner's APPROVED Facebook source Page.

Read-only reconnaissance on the already running ONE Kaggle Chrome via CDP.
Never launch Chrome, download media, post, bypass login, or declare source
retrieval successful just because a video link/card exists.

Official source: Mẹo nhỏ mỗi ngày (owner-selected). The Page share URL is
resolved by the browser under the user's actual profile session. Extract only
safe public-facing video links and optional on-page date hints, no cookies,
account text, tokens, raw captions or login form contents.

This bounded verification does not advance any job to SOURCE_READY: stage 05
requires actual MP4 bytes + sound + SHA-256, which is separate future work.
"""
from __future__ import annotations

from datetime import datetime, timezone
import asyncio
import json
import os
import subprocess
from pathlib import Path
import re
import sys
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from money_ai.chrome import Chrome
from money_ai.facebook_video_urls import classify_video_link

SOURCE_PAGE = "https://www.facebook.com/share/1AJ3zx9Uke/"
PAGE_NAME = "Mẹo nhỏ mỗi ngày"
REPORT = Path("/kaggle/working/tool-system-v2-runtime/source_inspection.json")
VIDEO_PATH = re.compile(r"(?:/reel/|/videos/|/watch/|/watch\?|fb\.watch/)", re.I)

JS = """els => els.slice(0, 700).map(el => {
  const href = el.href || '';
  const label = (el.innerText || el.getAttribute('aria-label') || '').slice(0, 140);
  const time = el.closest('article,div[role=article]')?.querySelector('time')?.getAttribute('datetime') || '';
  return {href, label, time};
})"""

def sanitize_candidates(raw: list[dict]) -> list[dict[str, str]]:
    """Strict allowlist and no URL credentials or tokens."""
    out, seen = [], set()
    for item in raw:
        href = str(item.get("href", ""))
        parsed = urlparse(href)
        if (parsed.scheme != "https" or (parsed.hostname or "").lower()
            not in {"www.facebook.com", "facebook.com", "m.facebook.com", "fb.watch"}
            or parsed.username or parsed.password
            or not VIDEO_PATH.search(parsed.path + (("?" + parsed.query) if parsed.query else ""))):
            continue
        # A Page's generic Videos/Reels navigation links are not videos.
        if classify_video_link(href).video_url is None:
            continue
        url = f"https://{parsed.hostname}{parsed.path}"
        # Facebook watch URLs often identify the video only in ?v=ID.
        # Retain that one identifier but discard tracking/auth query values.
        if parsed.path.rstrip("/") == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
            if not re.fullmatch(r"[0-9]{5,30}", video_id):
                continue
            url += "?v=" + video_id
        if url in seen:
            continue
        seen.add(url)
        out.append({"url": url,
                    "date_hint": str(item.get("time", ""))[:32],
                    "label_hint": str(item.get("label", ""))[:140]})
        if len(out) >= 30:
            break
    return out


def inspect(page):
    """Owner login and redirect checks never try to enter credentials."""
    page.goto(SOURCE_PAGE, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2400)
    url = page.url
    host = (urlparse(url).hostname or "").lower()
    if host not in {"www.facebook.com", "facebook.com", "m.facebook.com"}:
        return "UNEXPECTED_SOURCE_REDIRECT", []
    if "/login" in url.lower() or page.locator('input[type="password"]').count():
        return "OWNER_FACEBOOK_LOGIN_REQUIRED", []
    found = []
    for _ in range(3):
        raw = page.locator("a[href]").evaluate_all(JS)
        found = sanitize_candidates(raw)
        if found:
            break
        page.mouse.wheel(0, 680)
        page.wait_for_timeout(800)
    if not found:
        return "PAGE_LOADED_NO_VIDEO_LINKS", []
    return "VIDEO_CANDIDATES_FOUND_DOWNLOAD_UNVERIFIED", found


def _inspect_sync() -> dict:
    """Perform one bounded read-only CDP inspection outside Jupyter's event loop."""
    os.environ["DISPLAY"] = ":99"
    with Chrome("/kaggle/working/tool-system-v2-runtime/chrome_profile",
                "/kaggle/working/tool-system-v2-runtime/downloads",
                cdp_url="http://127.0.0.1:9222") as browser:
        if not browser._shared_browser:
            raise RuntimeError("SAME_CHROME_CDP_NOT_ATTACHED")
        ctx = browser.context
        if ctx is None:
            raise RuntimeError("CHROME_CONTEXT_NOT_AVAILABLE")
        tab = ctx.new_page()
        try:
            status, candidates = inspect(tab)
        except Exception as exc:
            # Explicit status rather than untrusted content and private URL.
            status = "PAGE_INSPECTION_ERROR_" + type(exc).__name__
            candidates = []
        finally:
            tab.close()  # Never close owner's own tabs.
    result = {
        "page": PAGE_NAME, "source_page": SOURCE_PAGE,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": status, "candidate_count": len(candidates),
        "candidates": candidates[:30],
        "earliest_video_verified": False,
        "source_mp4_download_verified": False,
        "owner_facebook_login_verified": False,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("TOOL_SYSTEM_SOURCE_PAGE=" + PAGE_NAME)
    print("TOOL_SYSTEM_SOURCE_INSPECTION=" + status)
    print("TOOL_SYSTEM_VIDEO_CANDIDATES=" + str(len(candidates)))
    print("TOOL_SYSTEM_SOURCE_MP4=NOT_DOWNLOADED")
    print("TOOL_SYSTEM_REPORT=" + str(REPORT))
    return result


def _sanitized_browser_env() -> dict[str, str]:
    """Only include variables needed to attach to local Chrome.

    Never send Groq, Supabase, Github, Telegram or account secrets to this
    separate source-inspection process. Browser uses its existing profile.
    """
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/root"),
        "DISPLAY": ":99",
        "PYTHONPATH": str(ROOT / "src"),
    }


def main() -> dict | None:
    """Work both from Kaggle runpy Notebook and independent CLI execution.

    Jupyter has a running asyncio event loop: Playwright's synchronous API
    raises if called from it. Use exactly one subprocess that ATTACHES through
    CDP to the already-open Chrome, never another Chrome or desktop process.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _inspect_sync()
    process = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_sanitized_browser_env(),
        timeout=100,
        check=False,
    )
    output = process.stdout.strip()
    if process.returncode or "TOOL_SYSTEM_SOURCE_INSPECTION=" not in output:
        # This is an explicit error, not a fake "no video found".
        # Never print full Playwright HTTP/URL exception logs from worker.
        msg = output[-180:] if output else f"WORKER_EXIT_{process.returncode}"
        raise RuntimeError("FACEBOOK_SOURCE_WORKER_FAILED: " + msg)
    print(output)
    return None


if __name__ == "__main__":
    if "--worker" in sys.argv:
        try:
            _inspect_sync()
        except Exception as exc:
            print("TOOL_SYSTEM_SOURCE_WORKER_ERROR=" + type(exc).__name__, flush=True)
            raise SystemExit(1) from None
    else:
        main()
