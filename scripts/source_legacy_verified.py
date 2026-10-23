"""Facebook source recovery using Money Agent V2's VERIFIED previous workflow.

Read-only reference in legacy repository:
  services/manager/page_video_sources.py      # resolved owner share URL
  services/manager/facebook_oldest_first.py    # canonical Page /reels; scroll grid
  services/manager/money_codespace_source.py   # signed-in video.currentSrc then yt-dlp
  services/manager/source_evidence/four_page_owner_crawl_20261013.json

These were already validated on a DIFFERENT 2026-10-13 environment:
  source: https://www.facebook.com/Funnyclip2026
  video:  https://www.facebook.com/reel/1208823671831153
  H264+AAC, duration 26.839s, SHA256 52837f89... (original rendition).
This file does not use legacy credentials/runtimes or pretend the old binary
exists on this NEW Kaggle machine. Connect to existing CDP 9222 ONLY.

The selected video is oldest among *loaded* Reels, never certified as the
absolute earliest-ever upload. No login bypass, cookies, no publication.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
for d in (ROOT, ROOT / "src"):
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))
from money_ai.chrome import Chrome
from money_ai.core import digest_file
from money_ai.facebook_video_urls import classify_video_link
from scripts.download_first_source import _probe_audio
from scripts.download_source_ytdlp import (
    check_media_tools, ensure_ytdlp, download_public_candidate
)

PAGE_APPROVED_SHARE = "https://www.facebook.com/share/1AJ3zx9Uke/"
PAGE_CANONICAL = "https://www.facebook.com/Funnyclip2026"
PAGE_REELS = PAGE_CANONICAL + "/reels"
REFERENCE_REEL = "https://www.facebook.com/reel/1208823671831153"
REFERENCE_SHA256 = "52837f89bba6189b5541bb55218e84d95173fc5320ef8f3791ba15e6a09d5f76"
REPORT = Path("/kaggle/working/tool-system-v2-runtime/source_legacy_pattern.json")
MEDIA_DIR = Path("/kaggle/working/tool-system-v2-runtime/sources")
MAX_BYTES = 120 * 1024 * 1024
MAX_SCROLLS = 18
STABLE_SCROLLS = 5
MAX_REELS_TO_TRY = 2
REEL_RE = re.compile(r"^https://www\.facebook\.com/reel/[0-9]{8,25}$")


def valid_source_media(url: str) -> bool:
    """Exactly HTTPS Facebook CDN, no untrusted hosts; no .mp4 suffix required."""
    if not isinstance(url, str):
        return False
    try:
        p = urlsplit(url)
    except ValueError:
        return False
    h = (p.hostname or "").lower()
    return (p.scheme == "https" and not p.username and not p.password and
            h.endswith(".fbcdn.net") and h != "fbcdn.net")


def loaded_reels(page) -> list[str]:
    raw = page.locator('a[href*="/reel/"]').evaluate_all(
        "(els) => els.map(e => e.href || '').slice(0,3000)")
    out, seen = [], set()
    for href in raw:
        result = classify_video_link(href)
        if result.kind != "REEL_POST" or not result.video_url:
            continue
        if result.video_url not in seen:
            seen.add(result.video_url)
            out.append(result.video_url)
    return out


def choose_from_legacy_grid(page) -> tuple[list[str], dict]:
    """Reuse earlier OLD-TO-NEW rendered Reels grid strategy, bounded.

    Facebook's rendered grid order is not an authoritative publication date.
    """
    page.goto(PAGE_REELS, wait_until="domcontentloaded", timeout=35000)
    page.wait_for_timeout(1800)
    dest = urlsplit(page.url)
    if (dest.hostname not in {"www.facebook.com","facebook.com","m.facebook.com"}
            or "/login" in page.url.lower()
            or page.locator('input[type="password"]').count()):
        return [], {"status": "FACEBOOK_SOURCE_LOGIN_REQUIRED"}
    if "/reels" not in dest.path:
        return [], {"status": "FACEBOOK_CANONICAL_REELS_NOT_ACCESSIBLE"}
    prev, stable, reels = 0, 0, []
    for _ in range(MAX_SCROLLS):
        # Some UI variants virtualize the grid. Preserve cumulative list.
        current = loaded_reels(page)
        seen = set(reels)
        reels.extend(u for u in current if u not in seen)
        stable = stable + 1 if len(reels) == prev else 0
        prev = len(reels)
        if stable >= STABLE_SCROLLS:
            break
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(550)
    # Rightmost/bottom tiles are typically older, as the old V2 scraper did.
    candidates = list(reversed(reels))
    return candidates, {
        "status": ("REELS_GRID_LOADED" if reels else "REELS_GRID_NO_POSTS"),
        "loaded_reel_count": len(reels),
        "grid_exhaustion_observed": stable >= STABLE_SCROLLS,
        "absolute_oldest_verified": False,
    }


def _existing_media(tab) -> str:
    if not tab.locator("video").count():
        return ""
    try:
        # Code from money_codespace_source.py, without its old CDP/runtimes.
        return str(tab.locator("video").first.evaluate(
            "(v) => v.currentSrc || v.src || ''") or "")
    except Exception:
        return ""


def _grab_cd_n(url: str, target: Path) -> Path:
    """Stream a complete plain video file from Chrome's own currentSrc.

    Facebook URLs often have opaque paths, so verify Content-Type and ffprobe
    rather than demanding a literal .mp4 suffix. No browser cookie export.
    """
    if not valid_source_media(url):
        raise RuntimeError("MEDIA_NOT_DIRECT_FB_CDN")
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(".part")
    try:
        with requests.get(url, stream=True, timeout=(12, 45),
                          headers={"User-Agent": "Mozilla/5.0"}) as r:
            if r.status_code != 200:
                raise RuntimeError("MEDIA_HTTP_NOT_200")
            ctype = str(r.headers.get("Content-Type") or "").lower()
            if ("video/mp4" not in ctype and
                "application/octet-stream" not in ctype):
                raise RuntimeError("MEDIA_NOT_MP4_RESPONSE")
            if r.headers.get("Content-Range"):
                raise RuntimeError("MEDIA_IS_PARTIAL_SEGMENT")
            header_length = r.headers.get("Content-Length") or ""
            if header_length.isdigit() and int(header_length) > MAX_BYTES:
                raise RuntimeError("MEDIA_FILE_TOO_LARGE")
            count = 0
            with part.open("wb") as out:
                for block in r.iter_content(512 * 1024):
                    if block:
                        count += len(block)
                        if count > MAX_BYTES:
                            raise RuntimeError("MEDIA_FILE_TOO_LARGE")
                        out.write(block)
            if count < 3000 or (header_length.isdigit() and count != int(header_length)):
                raise RuntimeError("MEDIA_FILE_INCOMPLETE")
        part.replace(target)
        return target
    finally:
        part.unlink(missing_ok=True)


def _store_verified(path: Path, reel_url: str, method: str) -> dict:
    if not path.is_file() or not 3000 <= path.stat().st_size <= MAX_BYTES:
        raise RuntimeError("MEDIA_BYTES_INVALID")
    av = _probe_audio(path)  # Both video and audio must be verified.
    sha = digest_file(path)
    dest = MEDIA_DIR / (sha + ".mp4")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file():
        if digest_file(dest) != sha:
            raise RuntimeError("MEDIA_STORED_SHA_MISMATCH")
    else:
        # Copy from temporary directory, don't touch other browser files.
        import shutil
        shutil.copyfile(path, dest)
    if digest_file(dest) != sha:
        raise RuntimeError("MEDIA_COPY_SHA_MISMATCH")
    return {
        "mp4_path": str(dest), "sha256": sha,
        "bytes": dest.stat().st_size, "duration_seconds": av["duration"],
        "audio": True, "source_reel_url": reel_url, "method": method,
        "matches_20261013_reference_sha": sha == REFERENCE_SHA256,
    }


def run_once() -> dict:
    os.environ["DISPLAY"] = ":99"
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    attempted = []
    selected = None
    with Chrome("/kaggle/working/tool-system-v2-runtime/chrome_profile",
                "/kaggle/working/tool-system-v2-runtime/downloads",
                cdp_url="http://127.0.0.1:9222") as browser:
        if browser._shared_browser is None or browser.context is None:
            raise RuntimeError("SHARED_OWNER_CHROME_REQUIRED")
        tab = browser.context.new_page()
        try:
            grid, evidence = choose_from_legacy_grid(tab)
            print("TOOL_SYSTEM_LEGACY_PAGE=" + PAGE_CANONICAL, flush=True)
            print("TOOL_SYSTEM_REELS_LOADED=" +
                  str(evidence.get("loaded_reel_count",0)), flush=True)
            if evidence["status"] == "FACEBOOK_SOURCE_LOGIN_REQUIRED":
                status = "FACEBOOK_LOGIN_REQUIRED"
                targets = []
            else:
                # Sample the oldest *loaded* Reel first, with one previously
                # VERIFIED reference Reel as a bounded fallback for diagnostics.
                # FIRST: oldest loaded Reel. SECOND: previously verified
                # example from SAME owner-approved Page, if different.
                targets = grid[:1] if grid else []
                if REFERENCE_REEL not in targets:
                    targets.append(REFERENCE_REEL)
                status = "NO_VERIFIED_SOURCE_MP4"
            for index, reel in enumerate(targets[:MAX_REELS_TO_TRY],1):
                print("TOOL_SYSTEM_REEL_ATTEMPT=" + str(index), flush=True)
                try:
                    tab.goto(reel, wait_until="domcontentloaded", timeout=30000)
                    tab.wait_for_timeout(2700)
                    if ("/login" in tab.url.lower()
                        or tab.locator('input[type="password"]').count()):
                        attempted.append("FACEBOOK_REEL_LOGIN_REQUIRED")
                        continue
                    source = _existing_media(tab)
                    if not valid_source_media(source):
                        # Old workflow navigated to Reel then read the already
                        # playing video's currentSrc. Never expose signed URL.
                        attempted.append("NO_ORDINARY_FB_CDN_VIDEO_SOURCE")
                    else:
                        with TemporaryDirectory(dir=MEDIA_DIR) as tmp:
                            try:
                                path = _grab_cd_n(source, Path(tmp) / "clip.mp4")
                                selected = _store_verified(
                                    path, reel, "LEGACY_VIDEO_CURRENT_SRC")
                                status = "REAL_MP4_WITH_AUDIO_VERIFIED"
                                break
                            except Exception as e:
                                attempted.append("FB_CDN_" + type(e).__name__)
                except Exception as e:
                    attempted.append("FACEBOOK_REEL_" + type(e).__name__)
                # Fallback adopted from legacy code: only the exact Reel, with
                # ordinary public yt-dlp and ffmpeg audio/video merge.
                try:
                    check_media_tools()
                    ensure_ytdlp()
                    with TemporaryDirectory(dir=MEDIA_DIR) as tmp:
                        dl_path, result_code = download_public_candidate(
                            reel, Path(tmp))
                        if dl_path is None:
                            attempted.append(result_code)
                        else:
                            selected = _store_verified(
                                dl_path, reel, "LEGACY_PUBLIC_YTDLP")
                            status = "REAL_MP4_WITH_AUDIO_VERIFIED"
                            break
                except Exception as e:
                    attempted.append("YTDLP_FALLBACK_" + type(e).__name__)
            if selected:
                print("TOOL_SYSTEM_LEGACY_SOURCE_MP4=" +
                      selected["mp4_path"], flush=True)
                print("TOOL_SYSTEM_LEGACY_SOURCE_SHA256=" +
                      selected["sha256"], flush=True)
                print("TOOL_SYSTEM_LEGACY_SOURCE_AUDIO=VERIFIED", flush=True)
        finally:
            tab.close()
    result = {
        "source_page": PAGE_CANONICAL,
        "approved_share_url": PAGE_APPROVED_SHARE,
        "source_reel": selected["source_reel_url"] if selected else None,
        "status": status,
        "grid_evidence": evidence,
        "selected": selected,
        "earliest_video_verified": False,
        "source_usage_rights_verified": False,
        "attempt_reasons": attempted[:12],
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print("TOOL_SYSTEM_LEGACY_PATTERN_RESULT=" + status, flush=True)
    if not selected:
        for i, a in enumerate(attempted,1):
            print(f"TOOL_SYSTEM_LEGACY_ATTEMPT_{i}={a}", flush=True)
    print("TOOL_SYSTEM_LEGACY_REPORT=" + str(REPORT), flush=True)
    return result


def main() -> None:
    # Kaggle Notebook runs asyncio; Sync Playwright must be in a child.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        run_once()
        return
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/root"),
        "DISPLAY": ":99",
        "PYTHONPATH": str(ROOT / "src"),
    }
    print("TOOL_SYSTEM_LEGACY_PATTERN_RUNNING: one existing Chrome; 2 Reels max",
          flush=True)
    child = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker"],
        capture_output=True, text=True, timeout=340,
        env=env, check=False
    )
    if child.returncode:
        print(child.stdout[-500:])
        raise RuntimeError("LEGACY_SOURCE_RECOVERY_WORKER_FAILED")
    print(child.stdout.strip())


if __name__ == "__main__":
    if "--worker" in sys.argv:
        try:
            run_once()
        except Exception as exc:
            print("TOOL_SYSTEM_LEGACY_FATAL=" + type(exc).__name__, flush=True)
            raise SystemExit(1) from None
    else:
        main()
