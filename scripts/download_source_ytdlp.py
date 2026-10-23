"""Try a public Facebook source video with yt-dlp's supported extractor.

Tool System V2 has already discovered candidate Facebook URLs using the owner's
ONE Chrome (CDP). This bounded fallback uses yt-dlp/ffmpeg as a CLI only; it
does not create another browser, bypass login, use browser cookies, or post.

When Facebook publishes independent video+audio streams, yt-dlp can merge
ordinary accessible formats into a complete MP4. Never claim success without
checking actual local bytes, BOTH media streams, duration and SHA-256.

First successful public sample is NOT necessarily the earliest source video.
Rights to reuse source material must still be checked before publication.
"""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
# Kaggle invokes this file through runpy.run_path from a Notebook whose
# working directory is NOT the checked-out release. The repository root must
# be importable for 'scripts.download_first_source' (namespace package).
# Always insert both paths before importing sibling scripts.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
from money_ai.core import digest_file
from money_ai.facebook_video_urls import classify_video_link
from scripts.download_first_source import (
    INPUT, MEDIA_DIR, _approved_candidates, _probe_audio, order_candidates
)

REPORT = Path("/kaggle/working/tool-system-v2-runtime/source_ytdlp.json")
MAX_CANDIDATES = 3
MAX_BYTES = 120 * 1024 * 1024
INSTALL_TIMEOUT = 100
DOWNLOAD_TIMEOUT = 110
ERROR_CODES = (
    ("facebook_parse_error", ("cannot parse data", "unable to extract")),
    ("rate_limit", ("http error 429", "too many requests")),
    ("authentication_required", ("login required", "sign in", "log in",
                                 "login to", "not logged in")),
    ("not_public", ("private video", "not available in your country",
                    "members only", "content isn't available")),
    ("forbidden", ("http error 403", "forbidden")),
    ("unsupported", ("unsupported url", "no suitable extractor")),
    ("format_unavailable", ("requested format is not available",
                            "no video formats found")),
    ("ffmpeg_failure", ("ffmpeg", "merging formats failed")),
)


def classify_failure(output: str, returncode: int) -> str:
    """Report only stable categories, never raw signed URLs or cookies."""
    lowered = output.lower()
    for category, pieces in ERROR_CODES:
        if any(term in lowered for term in pieces):
            return "YTDLP_" + category.upper()
    if returncode == -1:
        return "YTDLP_TIMEOUT"
    return "YTDLP_EXIT_" + str(returncode)


def check_media_tools() -> None:
    """Restore ffmpeg only if missing, never restart Chrome or Notebook."""
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    apt = shutil.which("apt-get")
    if not apt or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeError("SOURCE_FFMPEG_OR_FFPROBE_MISSING")
    print("TOOL_SYSTEM_FFMPEG_INSTALLING_ONCE", flush=True)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "DEBIAN_FRONTEND": "noninteractive"}
    for cmd, timeout in (([apt, "-qq", "update"], 140),
                         ([apt, "-y", "-qq", "install",
                           "--no-install-recommends", "ffmpeg"], 180)):
        try:
            result = subprocess.run(cmd, capture_output=True,
                                    text=True, timeout=timeout,
                                    env=env, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("SOURCE_FFMPEG_INSTALL_TIMEOUT") from None
        if result.returncode:
            raise RuntimeError("SOURCE_FFMPEG_INSTALL_FAILED")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("SOURCE_FFMPEG_STILL_MISSING")
    print("TOOL_SYSTEM_FFMPEG_READY", flush=True)


def ensure_ytdlp() -> None:
    """Install only lightweight public extractor if absent; not a new service."""
    if importlib.util.find_spec("yt_dlp") is not None:
        return
    print("TOOL_SYSTEM_YTDLP_INSTALLING_ONCE", flush=True)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
             "--quiet", "yt-dlp"],
            capture_output=True, text=True, timeout=INSTALL_TIMEOUT, check=False
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("SOURCE_YTDLP_INSTALL_TIMEOUT") from None
    if result.returncode or importlib.util.find_spec("yt_dlp") is None:
        raise RuntimeError("SOURCE_YTDLP_INSTALL_FAILED")
    print("TOOL_SYSTEM_YTDLP_READY", flush=True)


def download_public_candidate(url: str, workdir: Path,
                              run=subprocess.run) -> tuple[Path | None, str]:
    """One bounded extraction with video+audio merge, no browser cookies.

    Stable --output directs all yt-dlp byproducts into a temporary directory.
    --print after_move:filepath reveals local filename only on success.
    No stderr is retained, as Facebook CDN URLs can contain signed tokens.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    output = workdir / "source.%(ext)s"
    argv = [
        sys.executable, "-m", "yt_dlp",
        "--ignore-config", "--no-playlist", "--no-progress", "--no-warnings",
        "--quiet", "--no-overwrites", "--no-part",
        "--format", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "--remux-video", "mp4",
        "--max-filesize", str(MAX_BYTES),
        "--socket-timeout", "20", "--retries", "1",
        "--fragment-retries", "1",
        "--print", "after_move:filepath",
        "--output", str(output),
        "--", url,
    ]
    try:
        result = run(argv, capture_output=True, text=True,
                     timeout=DOWNLOAD_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        return None, "YTDLP_TIMEOUT"
    if result.returncode:
        return None, classify_failure(
            (result.stderr or "")[-4000:], result.returncode)
    candidates = [Path(line.strip()) for line in (result.stdout or "").splitlines()
                  if line.strip().endswith(".mp4")]
    if len(candidates) != 1:
        return None, "YTDLP_NO_MP4_OUTPUT"
    path = candidates[0]
    try:
        if (not path.is_file()
                or not path.resolve().is_relative_to(workdir.resolve())
                or path.stat().st_size < 2048
                or path.stat().st_size > MAX_BYTES):
            return None, "YTDLP_FILE_INVALID"
    except (OSError, ValueError):
        return None, "YTDLP_FILE_INVALID"
    return path, "YTDLP_DOWNLOADED_PENDING_AUDIO_VERIFICATION"


def run() -> dict:
    """Bounded up to 3 public candidates; never declare oldest or approval."""
    raw = json.loads(INPUT.read_text(encoding="utf-8"))
    # Validate source identity/status with the existing approved-source gate,
    # but keep rejected hub-link metadata for useful diagnostics.
    _approved_candidates(raw)
    candidates = [item for item in raw.get("candidates", [])
                  if isinstance(item, dict) and isinstance(item.get("url"), str)]
    if not candidates:
        raise RuntimeError("SOURCE_INSPECTION_HAS_NO_CANDIDATES")
    ordered, _ = order_candidates(candidates)

    # The original Facebook crawler accepted navigation /videos/ links as
    # video candidates. They can never yield an MP4; check actual post IDs
    # and normalize valid permalinks before calling any extractor.
    valid, invalid = [], []
    for n, item in enumerate(ordered, 1):
        kind = classify_video_link(item["url"])
        if kind.video_url is None:
            invalid.append({"candidate": n, "status": "INVALID_" + kind.kind})
        else:
            valid.append({"url": kind.video_url,
                          "candidate": n, "date_hint": item.get("date_hint", "")})
    print("TOOL_SYSTEM_VALID_VIDEO_POST_LINKS=" + str(len(valid)), flush=True)
    print("TOOL_SYSTEM_REJECTED_NAVIGATION_LINKS=" + str(len(invalid)), flush=True)

    status = "PUBLIC_MEDIA_UNAVAILABLE" if valid else "NO_VERIFIABLE_VIDEO_POST_LINKS"
    selected = None
    attempts = list(invalid)
    if valid:
        check_media_tools()
        ensure_ytdlp()
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    for index, item in enumerate(valid[:MAX_CANDIDATES], 1):
        print(f"TOOL_SYSTEM_PUBLIC_SOURCE_CHECK={index}/{min(MAX_CANDIDATES, len(valid))}",
              flush=True)
        with TemporaryDirectory(prefix=".money-ai-source-", dir=MEDIA_DIR) as folder:
            filename, failure = download_public_candidate(
                item["url"], Path(folder))
            if filename is None:
                attempts.append({"candidate": item["candidate"], "status": failure})
                continue
            try:
                proof = _probe_audio(filename)
                checksum = digest_file(filename)
                stored = MEDIA_DIR / (checksum + ".mp4")
                if stored.exists():
                    if digest_file(stored) != checksum:
                        raise RuntimeError("SOURCE_EXISTING_HASH_MISMATCH")
                else:
                    shutil.copyfile(filename, stored)
                if digest_file(stored) != checksum:
                    raise RuntimeError("SOURCE_LOCAL_SHA_MISMATCH")
                selected = {
                    "source_video_url": item["url"],
                    "mp4_path": str(stored),
                    "sha256": checksum,
                    "bytes": stored.stat().st_size,
                    "duration_seconds": proof["duration"],
                    "audio": True,
                }
                status = "REAL_MP4_WITH_AUDIO_VERIFIED"
                break
            except RuntimeError as exc:
                code = str(exc).split(":")[0].strip()
                permitted = {"SOURCE_FFPROBE_FAILED", "SOURCE_AUDIO_MISSING",
                             "SOURCE_VIDEO_STREAM_MISSING",
                             "SOURCE_DURATION_INVALID",
                             "SOURCE_LOCAL_SHA_MISMATCH",
                             "SOURCE_EXISTING_HASH_MISMATCH"}
                attempts.append({"candidate": item["candidate"], "status":
                                 code if code in permitted else
                                 "YTDLP_MEDIA_VALIDATION_FAILED"})
            except (OSError, ValueError, subprocess.SubprocessError):
                attempts.append({"candidate": item["candidate"],
                                 "status": "YTDLP_MEDIA_VALIDATION_FAILED"})

    report = {
        "page": "Mẹo nhỏ mỗi ngày",
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "selected": selected,
        "earliest_video_verified": False,
        "copyright_permission_verified": False,
        "attempted_candidates": len(attempts) + (1 if selected else 0),
        "valid_post_links": len(valid),
        "rejected_navigation_links": len(invalid),
        "attempts": attempts,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print("TOOL_SYSTEM_YTDLP_SOURCE=" + status, flush=True)
    print("TOOL_SYSTEM_SOURCE_AUDIO=" + (
        "VERIFIED" if selected else "NOT_VERIFIED"), flush=True)
    if selected:
        print("TOOL_SYSTEM_SOURCE_MP4_PATH=" + selected["mp4_path"], flush=True)
        print("TOOL_SYSTEM_SOURCE_SHA256=" + selected["sha256"], flush=True)
        print("TOOL_SYSTEM_SOURCE_DURATION=" +
              str(selected["duration_seconds"]), flush=True)
    else:
        for item in attempts:
            print("TOOL_SYSTEM_CANDIDATE_" +
                  str(item["candidate"]) + "=" + item["status"], flush=True)
    print("TOOL_SYSTEM_YTDLP_REPORT=" + str(REPORT), flush=True)
    return report


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        # Do not print network error text with signed access parameters.
        print("TOOL_SYSTEM_YTDLP_FATAL=" + type(exc).__name__, flush=True)
        print("TOOL_SYSTEM_PUBLIC_SOURCE_CHECK=FAILED", flush=True)
        raise SystemExit(1) from None
