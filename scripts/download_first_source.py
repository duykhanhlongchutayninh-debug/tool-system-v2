"""Step 05: obtain actual Facebook source MP4 via the owner's existing Chrome.

The source inspection must already contain real candidate URLs from the approved
Facebook source Page. Use the SAME CDP Chrome context and temporary tabs.
This does NOT use a separate downloader account, bypass login, access cookies,
touch Meta/Gemini tabs, publish or submit anything.

Source content is treated as inspiration for permitted transformations, subject
to the owner's right to use it. A video candidate is NOT proof of an MP4.

Try the browser's own media/download surfaces only. Stop safely if Facebook
does not expose an ordinary playable MP4 URL; no DRM or bypass workarounds.
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
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from money_ai.chrome import Chrome
from money_ai.facebook_video_urls import classify_video_link
from money_ai.core import digest_file

INPUT = Path("/kaggle/working/tool-system-v2-runtime/source_inspection.json")
REPORT = Path("/kaggle/working/tool-system-v2-runtime/source_download.json")
MEDIA_DIR = Path("/kaggle/working/tool-system-v2-runtime/sources")
MAX_BYTES = 120 * 1024 * 1024
MAX_CANDIDATES = 7
FB_HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com", "fb.watch"}
CDN_SUFFIXES = (".fbcdn.net", ".cdninstagram.com")
MEDIA_JS = """() => {
 const out = [];
 for (const el of document.querySelectorAll('video,source')) {
   for (const prop of ['src', 'currentSrc']) {
     const src = el[prop] || el.getAttribute(prop);
     if (src) out.push(src);
   }
 }
 for (const sel of ['meta[property="og:video"]',
                    'meta[property="og:video:url"]',
                    'meta[property="og:video:secure_url"]']) {
   for (const el of document.querySelectorAll(sel)) {
     if (el.content) out.push(el.content);
   }
 }
 // PerformanceResourceTiming reveals already fetched media even if the
 // video element is a MediaSource blob (no direct src visible).
 for (const item of performance.getEntriesByType('resource').slice(-350)) {
   if (item && item.name) out.push(item.name);
 }
 return out.slice(0,400);
}"""
DATE_JS = """() => [
 ...document.querySelectorAll('time[datetime]'),
 ...document.querySelectorAll('meta[property="article:published_time"]')
].map(el => el.getAttribute('datetime') || el.content || '').filter(Boolean).slice(0,15)"""

def _media_allowed(url: str) -> bool:
    """Only explicitly delivered Facebook CDN HTTPS MP4 media URLs."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    if not any(host.endswith(suffix) for suffix in CDN_SUFFIXES):
        return False
    # Do not accept .m3u8, DASH manifests, DRM, javascript or blob.
    return bool(re.search(r"\.mp4(?:$|[?])", parsed.path + ("?" + parsed.query if parsed.query else ""), re.I))


def _approved_candidates(data: dict) -> list[dict[str, str]]:
    if data.get("source_page") != "https://www.facebook.com/share/1AJ3zx9Uke/":
        raise RuntimeError("SOURCE_PAGE_NOT_APPROVED")
    if data.get("status") != "VIDEO_CANDIDATES_FOUND_DOWNLOAD_UNVERIFIED":
        raise RuntimeError("SOURCE_CANDIDATES_NOT_VERIFIED")
    records = data.get("candidates") or []
    verified = []
    for item in records:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", ""))
        parsed = urlparse(url)
        if parsed.scheme != "https" or (parsed.hostname or "") not in FB_HOSTS:
            continue
        if classify_video_link(url).video_url is not None:
            verified.append({"url": url, "date_hint": str(item.get("date_hint") or "")[:40]})
    return verified[:MAX_CANDIDATES]


def _parse_iso(text: str) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def order_candidates(records: list[dict[str,str]]) -> tuple[list[dict[str,str]], bool]:
    """Chronology only if *all* candidates have valid machine-readable timestamps.

    Otherwise keep observed order but mark oldest-unverified; do not claim an
    authoritative earliest video based on seven links from a scrolling sample.
    """
    dates = [_parse_iso(item.get("date_hint", "")) for item in records]
    if dates and all(dates):
        return sorted(records, key=lambda x: _parse_iso(x["date_hint"])), False
    return records, True


def _probe_audio(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type", "-of", "json", str(path)],
        capture_output=True, text=True, check=False, timeout=30,
    )
    if result.returncode:
        raise RuntimeError("SOURCE_FFPROBE_FAILED")
    data = json.loads(result.stdout)
    codecs = {str(s.get("codec_type")) for s in data.get("streams", [])}
    if "video" not in codecs:
        raise RuntimeError("SOURCE_VIDEO_STREAM_MISSING")
    if "audio" not in codecs:
        raise RuntimeError("SOURCE_AUDIO_MISSING")
    duration = float(data.get("format", {}).get("duration") or 0)
    if not 0 < duration < 14400:
        raise RuntimeError("SOURCE_DURATION_INVALID")
    return {"duration": round(duration, 2), "audio": True}


def _fetch_standard_mp4(url: str, destination: Path, referrer: str,
                        opener=urlopen) -> Path:
    """Bounded standard HTTP GET; never forward Facebook cookies or CDN signatures.

    URLs can be time-limited; they are only used in memory and are never printed.
    A partial stream, HTML error page or oversized response cannot become a
    production asset.
    """
    if not _media_allowed(url):
        raise RuntimeError("SOURCE_NOT_STANDARD_FACEBOOK_MP4")
    req = Request(url, headers={"User-Agent": "Mozilla/5.0",
                                "Referer": referrer, "Accept": "video/mp4"})
    temp = destination.with_suffix(".partial")
    temp.parent.mkdir(parents=True, exist_ok=True)
    try:
        with opener(req, timeout=50) as res, temp.open("wb") as f:
            ctype = str(res.headers.get("Content-Type", "")).lower()
            if "video/mp4" not in ctype and "application/octet-stream" not in ctype:
                raise RuntimeError("SOURCE_NOT_MP4_RESPONSE")
            # Range responses may contain a valid MP4 header but not a full
            # video. Do not mistake a partial Facebook stream for a source MP4.
            if (getattr(res, "status", 200) != 200
                    or res.headers.get("Content-Range")):
                raise RuntimeError("SOURCE_PARTIAL_MEDIA_RESPONSE")
            length_header = res.headers.get("Content-Length", "") or ""
            if length_header.isdigit() and int(length_header) > MAX_BYTES:
                raise RuntimeError("SOURCE_MP4_TOO_LARGE")
            size = 0
            while True:
                chunk = res.read(256 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    raise RuntimeError("SOURCE_MP4_TOO_LARGE")
                f.write(chunk)
        if temp.stat().st_size < 2048:
            raise RuntimeError("SOURCE_MP4_EMPTY")
        if length_header.isdigit() and temp.stat().st_size != int(length_header):
            raise RuntimeError("SOURCE_MEDIA_LENGTH_MISMATCH")
        if destination.exists():
            raise RuntimeError("REFUSE_OVERWRITE_SOURCE")
        temp.replace(destination)
        return destination
    finally:
        temp.unlink(missing_ok=True)


def _find_media_in_tab(tab: Any) -> tuple[list[str], list[str]]:
    captured: list[str] = []
    def on_response(response):
        # Read only response metadata, never cookies or bodies.
        try:
            content_type = str(response.headers.get("content-type", "")).lower()
            url = response.url
            if _media_allowed(url) and "video/mp4" in content_type:
                captured.append(url)
        except Exception:
            return

    tab.on("response", on_response)
    try:
        tab.wait_for_timeout(3500)
        sources = tab.evaluate(MEDIA_JS)
        if tab.locator("video").count():
            try:
                tab.locator("video").first.click(timeout=3500)
                tab.wait_for_timeout(2200)
            except Exception:
                pass
        sources += tab.evaluate(MEDIA_JS)
        dates = tab.evaluate(DATE_JS)
        # Deduplicate while preserving observed order; raw signed URLs NEVER logged.
        urls = list(dict.fromkeys(u for u in sources + captured
                                  if isinstance(u, str) and _media_allowed(u)))
        return urls, dates
    finally:
        tab.remove_listener("response", on_response)


def _run_worker() -> dict:
    inspection = json.loads(INPUT.read_text(encoding="utf-8"))
    candidates = _approved_candidates(inspection)
    if not candidates:
        raise RuntimeError("NO_APPROVED_VIDEO_CANDIDATES")
    ordered, earliest_unverified = order_candidates(candidates)
    history: list[dict[str, str]] = []
    status = "NO_DIRECT_MP4_FOUND"
    selected: dict | None = None
    os.environ["DISPLAY"] = ":99"
    with Chrome("/kaggle/working/tool-system-v2-runtime/chrome_profile",
                "/kaggle/working/tool-system-v2-runtime/downloads",
                cdp_url="http://127.0.0.1:9222") as browser:
        ctx = browser.context
        if browser._shared_browser is None or ctx is None:
            raise RuntimeError("CHROME_CDP_REQUIRED")
        tab = ctx.new_page()
        try:
            for n, item in enumerate(ordered):
                url = item["url"]
                try:
                    tab.goto(url, wait_until="domcontentloaded", timeout=35000)
                    host = (urlparse(tab.url).hostname or "").lower()
                    if host not in FB_HOSTS or "/login" in tab.url.lower():
                        history.append({"candidate": str(n + 1),
                                        "status": "LOGIN_OR_REDIRECT"})
                        continue
                    if tab.locator('input[type="password"]').count():
                        history.append({"candidate": str(n + 1),
                                        "status": "FACEBOOK_LOGIN_REQUIRED"})
                        continue
                    media_urls, dates = _find_media_in_tab(tab)
                    if not media_urls:
                        history.append({"candidate": str(n + 1),
                                        "status": "NO_STANDARD_MP4_EXPOSED"})
                        continue
                    for candidate_url in media_urls[:3]:
                        local = MEDIA_DIR / f"sample_{n+1}.mp4"
                        try:
                            _fetch_standard_mp4(candidate_url, local, tab.url)
                            proof = _probe_audio(local)
                            digest = digest_file(local)
                            final = MEDIA_DIR / (digest + ".mp4")
                            if final.exists():
                                if digest_file(final) != digest:
                                    raise RuntimeError("SOURCE_FINAL_HASH_MISMATCH")
                                local.unlink(missing_ok=True)
                            else:
                                local.rename(final)
                            # Captions are not invented. We record only
                            # hashtag hints that the individual Reel exposes.
                            try:
                                meta = tab.locator('meta[property="og:description"]')
                                description = (meta.first.get_attribute(
                                    "content", timeout=2000)
                                    if meta.count() else "") or ""
                                hashtags = list(dict.fromkeys(re.findall(
                                    r"(?<![\w#])#[\w]+", description,
                                    flags=re.UNICODE)))[:30]
                            except Exception:
                                # Caption extraction is optional and must not
                                # invalidate an independently verified MP4.
                                hashtags = []
                            selected = {
                                "source_video_url": url,
                                "hashtags_hint": " ".join(hashtags),
                                "hashtags_verified": False,
                                "mp4_path": str(final),
                                "sha256": digest,
                                "bytes": final.stat().st_size,
                                "audio": True,
                                "duration_seconds": proof["duration"],
                                "published_time_hint": next(
                                    (v for v in dates if _parse_iso(v)), ""),
                            }
                            status = "REAL_MP4_WITH_AUDIO_VERIFIED"
                            break
                        except Exception as exc:
                            local.unlink(missing_ok=True)
                            history.append({"candidate": str(n + 1),
                                            "status": "MEDIA_ERROR_" + type(exc).__name__})
                    if selected:
                        break
                except Exception as exc:
                    history.append({"candidate": str(n + 1),
                                    "status": "PAGE_ERROR_" + type(exc).__name__})
        finally:
            tab.close()
    result = {
        "page": "Mẹo nhỏ mỗi ngày",
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "tested_candidates": len(history) + (1 if selected else 0),
        "earliest_video_verified": False,  # a sampled set cannot certify all older videos
        "oldest_of_sample_order_unverified": earliest_unverified,
        "selected": selected,
        "attempts": history[:30],
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print("TOOL_SYSTEM_SOURCE_DOWNLOAD=" + status, flush=True)
    print("TOOL_SYSTEM_SOURCE_AUDIO=" + ("VERIFIED" if selected else "NOT_VERIFIED"), flush=True)
    print("TOOL_SYSTEM_EARLIEST=" + "NOT_VERIFIED", flush=True)
    if selected:
        print("TOOL_SYSTEM_SOURCE_MP4_PATH=" + selected["mp4_path"], flush=True)
        print("TOOL_SYSTEM_SOURCE_SHA256=" + selected["sha256"], flush=True)
        print("TOOL_SYSTEM_SOURCE_DURATION=" + str(selected["duration_seconds"]), flush=True)
    else:
        print("TOOL_SYSTEM_SOURCE_ATTEMPTS=" + str(len(history)), flush=True)
    print("TOOL_SYSTEM_SOURCE_REPORT=" + str(REPORT), flush=True)
    return result


def _run_env() -> dict[str,str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/root"), "DISPLAY": ":99",
            "PYTHONPATH": str(ROOT / "src")}


def main() -> dict | None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run_worker()
    print("TOOL_SYSTEM_SOURCE_DOWNLOAD_RUNNING: checking up to 7 candidates, max 5.5 minutes", flush=True)
    print("Đang kiểm tra file MP4 thực tế. Không chạy lại ô Code trong lúc chờ.", flush=True)
    child = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker"],
        capture_output=True, text=True, timeout=330, env=_run_env(), check=False)
    if child.returncode:
        # Never expose private CDN signatures via stderr/traceback.
        raise RuntimeError("SOURCE_DOWNLOAD_WORKER_FAILED:" +
                           (child.stdout.strip()[-140:] or "CHILD_EXIT"))
    print(child.stdout.strip())
    return None


if __name__ == "__main__":
    if "--worker" in sys.argv:
        try:
            _run_worker()
        except Exception as exc:
            print("TOOL_SYSTEM_SOURCE_DOWNLOAD_FATAL=" + type(exc).__name__)
            raise SystemExit(1) from None
    else:
        main()
