"""Probe ONE real Facebook Reel already found by Tool System V2 (read-only).

Requires Kaggle's existing Chrome CDP 9222. No second Chrome, no cookies,
no prompts/posts, no media downloads, no direct signed-media URLs in output.
Purpose: after yt-dlp reports unsupported, distinguish a real playable
post from a login/consent or unavailable page before changing extraction.
"""
from __future__ import annotations
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from money_ai.chrome import Chrome
from money_ai.facebook_video_urls import classify_video_link

INSPECTION = Path("/kaggle/working/tool-system-v2-runtime/source_inspection.json")
REPORT = Path("/kaggle/working/tool-system-v2-runtime/reel_probe.json")
SOURCE = "https://www.facebook.com/share/1AJ3zx9Uke/"
FB_HOSTS = {"www.facebook.com", "facebook.com", "m.facebook.com"}
CDP_URL = "http://127.0.0.1:9222"

VIDEO_INFO = """() => [...document.querySelectorAll('video')].slice(0,8).map(el => ({
    ready: el.readyState, width: el.videoWidth, height: el.videoHeight,
    duration: Number.isFinite(el.duration) ? el.duration : null,
    hasAudioFlag: !!(el.mozHasAudio || (el.audioTracks && el.audioTracks.length)),
    sourceKind: (el.currentSrc || el.src || '').startsWith('blob:') ? 'BLOB'
       : (el.currentSrc || el.src || '').startsWith('https:') ? 'HTTPS'
       : 'NONE'
}))"""


def choose_reel(inspection: dict) -> tuple[str | None, dict[str, int]]:
    """Never mistake a Page's generic video menu for a specific Reel."""
    if inspection.get("source_page") != SOURCE:
        raise RuntimeError("SOURCE_PAGE_NOT_APPROVED")
    records = inspection.get("candidates", [])
    counts: Counter[str] = Counter()
    reels = []
    for row in records:
        if not isinstance(row, dict):
            continue
        candidate = classify_video_link(row.get("url"))
        counts[candidate.kind] += 1
        if candidate.kind == "REEL_POST" and candidate.video_url:
            reels.append(candidate.video_url)
    return (reels[0] if reels else None), dict(sorted(counts.items()))


def content_kind(content_type: str) -> str:
    mime = content_type.lower().split(";", 1)[0].strip()
    if mime.startswith("video/"): return "VIDEO"
    if mime.startswith("audio/"): return "AUDIO"
    if mime in {"application/dash+xml",
                "application/vnd.apple.mpegurl", "application/x-mpegurl"}:
        return "STREAM_MANIFEST"
    return "OTHER"


def inspect_reel(tab: Any, url: str) -> dict:
    """Inspect one genuine post on SAME Chrome, not an API or a new browser."""
    seen: Counter[str] = Counter()
    def response_observer(response):
        try:
            kind = content_kind(response.headers.get("content-type", ""))
            if kind != "OTHER":
                seen[kind] += 1
        except Exception:
            pass
    tab.on("response", response_observer)
    try:
        tab.goto(url, wait_until="domcontentloaded", timeout=35000)
        tab.wait_for_timeout(3500)
        host = (urlsplit(tab.url).hostname or "").lower()
        if host not in FB_HOSTS:
            return {"status": "UNEXPECTED_REDIRECT"}
        if ("/login" in tab.url.lower()
                or tab.locator('input[type="password"]').count()):
            return {"status": "FACEBOOK_LOGIN_REQUIRED"}
        before = tab.evaluate(VIDEO_INFO)
        # Only attempt to play the visible video once, never click page controls
        # such as Share/Publish/Login.
        if before and not any(int(v.get("ready") or 0) >= 2 for v in before):
            try:
                tab.locator("video").first.click(timeout=3000)
                tab.wait_for_timeout(2500)
            except Exception:
                pass
        after = tab.evaluate(VIDEO_INFO)
        playable = any(
            int(v.get("ready") or 0) >= 2
            and int(v.get("width") or 0) > 0
            and float(v.get("duration") or 0) > 0
            for v in after
        )
        return {
            "status": ("REEL_VIDEO_PLAYABLE_IN_BROWSER"
                       if playable else "REEL_PAGE_MEDIA_NOT_PROVEN"),
            "video_elements": len(after),
            "video_source_kinds": sorted({str(v.get("sourceKind"))
                                          for v in after}),
            "media_response_type_counts": dict(sorted(seen.items())),
            "audio_stream_verified": False,
            "source_mp4_downloaded": False,
            "oldest_post_verified": False,
        }
    finally:
        tab.remove_listener("response", response_observer)


def probe() -> dict:
    data = json.loads(INSPECTION.read_text(encoding="utf-8"))
    reel, categories = choose_reel(data)
    if reel is None:
        result = {"status":"NO_VERIFIED_REEL_POST_URL", "link_types":categories}
    else:
        os.environ["DISPLAY"] = ":99"
        with Chrome("/kaggle/working/tool-system-v2-runtime/chrome_profile",
                    "/kaggle/working/tool-system-v2-runtime/downloads",
                    cdp_url=CDP_URL) as browser:
            if browser._shared_browser is None or browser.context is None:
                raise RuntimeError("CHROME_CDP_NOT_ATTACHED")
            tab = browser.context.new_page()
            try:
                result = inspect_reel(tab, reel)
            finally:
                tab.close()
        result["link_types"] = categories
    result["checked_at_utc"] = datetime.now(timezone.utc).isoformat()
    result["source_page"] = SOURCE
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print("TOOL_SYSTEM_REEL_BROWSER=" + result["status"], flush=True)
    print("TOOL_SYSTEM_REEL_VIDEO_ELEMENTS=" +
          str(result.get("video_elements", 0)), flush=True)
    print("TOOL_SYSTEM_REEL_MEDIA_TYPES=" + json.dumps(
        result.get("media_response_type_counts", {}),
        sort_keys=True), flush=True)
    print("TOOL_SYSTEM_REEL_MP4=NOT_DOWNLOADED", flush=True)
    return result


def main() -> None:
    """Jupyter runs async; create one isolated worker, NOT a second browser."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        probe()
        return
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": os.environ.get("HOME", "/root"),
           "DISPLAY": ":99", "PYTHONPATH": str(ROOT / "src")}
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker"],
        env=env, capture_output=True, text=True, timeout=75, check=False)
    if proc.returncode:
        raise RuntimeError("REEL_BROWSER_PROBE_FAILED:" +
                           (proc.stdout.strip()[-100:] or "WORKER_ERROR"))
    print(proc.stdout.strip())


if __name__ == "__main__":
    if "--worker" in sys.argv:
        try:
            probe()
        except Exception as exc:
            print("TOOL_SYSTEM_REEL_PROBE_FATAL=" + type(exc).__name__, flush=True)
            raise SystemExit(1) from None
    else:
        main()
