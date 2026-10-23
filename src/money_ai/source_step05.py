"""Step 05: reuse existing verified source MP4 through the REAL Agent.

Never re-crawl a successful Facebook source or silently advance without a
source-bound caption and a real Supabase MP4 archive. This is the adapter
from source_legacy_verified.py into the existing Agent Tools.collect contract.

One Page / one verified Reel / one new SQLite job -> Supabase verified MP4 ->
Agent NEW -> SOURCE_READY, with DurableStore checkpoint on each transition.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from .agent import SourceResult
from .core import Job, digest_file
from .facebook_video_urls import classify_video_link

SOURCE_PAGE = "Mẹo nhỏ mỗi ngày"
SOURCE_CANONICAL = "https://www.facebook.com/Funnyclip2026"
SOURCE_APPROVED = "https://www.facebook.com/share/1AJ3zx9Uke/"
REPORT_PATH = Path("/kaggle/working/tool-system-v2-runtime/source_legacy_pattern.json")
CAPTION_PATH = Path("/kaggle/working/tool-system-v2-runtime/source_caption_evidence.json")
CAPTURE_WORKER = Path(__file__).resolve().parents[2] / "scripts" / "capture_first_hashtags.py"
SHA = re.compile(r"[a-f0-9]{64}\Z")
HASHTAG = re.compile(r"(?<![\w#])#[\w]{1,64}", re.UNICODE)
MAX_TAGS = 30
MAX_SOURCE = 50 * 1024 * 1024  # Supabase free per-object limit


class SourceEvidenceError(RuntimeError):
    pass


def tags_from_caption(text: str) -> list[str]:
    if not isinstance(text, str):
        return []
    found, seen = [], set()
    for match in HASHTAG.finditer(text[:6000]):
        tag = match.group(0)
        if tag.casefold() not in seen:
            seen.add(tag.casefold())
            found.append(tag)
        if len(found) >= MAX_TAGS:
            break
    return found


@dataclass(frozen=True)
class VerifiedSource:
    path: Path
    reel_url: str
    sha256: str
    size: int


def verified_local_source(report_path: Path = REPORT_PATH,
                          source_dir: Path = Path("/kaggle/working/tool-system-v2-runtime/sources")) -> VerifiedSource:
    """Fail closed if the old screenshot evidence and *current* bytes disagree."""
    if not report_path.is_file():
        raise SourceEvidenceError("STEP05_LEGACY_SOURCE_REPORT_MISSING")
    record = json.loads(report_path.read_text(encoding="utf-8"))
    if (record.get("source_page") != SOURCE_CANONICAL
            or record.get("approved_share_url") != SOURCE_APPROVED
            or record.get("status") != "REAL_MP4_WITH_AUDIO_VERIFIED"
            or record.get("earliest_video_verified") is True):
        raise SourceEvidenceError("STEP05_SOURCE_REPORT_UNVERIFIED")
    item = record.get("selected")
    if not isinstance(item, dict) or item.get("audio") is not True:
        raise SourceEvidenceError("STEP05_SOURCE_AUDIO_UNVERIFIED")
    link = classify_video_link(item.get("source_reel_url"))
    if link.kind != "REEL_POST":
        raise SourceEvidenceError("STEP05_REEL_LINK_INVALID")
    if (record.get("source_reel")
            and classify_video_link(record["source_reel"]).video_url != link.video_url):
        raise SourceEvidenceError("STEP05_SOURCE_REEL_MISMATCH")
    digest = item.get("sha256")
    if not isinstance(digest, str) or not SHA.fullmatch(digest):
        raise SourceEvidenceError("STEP05_SOURCE_SHA_MISSING")
    path = Path(str(item.get("mp4_path") or ""))
    folder = source_dir.resolve()
    if (not path.is_file() or path.suffix.lower() != ".mp4"
            or not path.resolve().is_relative_to(folder)
            or path.stat().st_size < 3000
            or path.stat().st_size > MAX_SOURCE):
        raise SourceEvidenceError("STEP05_SOURCE_MP4_MISSING_OR_TOO_LARGE")
    if digest_file(path) != digest or path.name != digest + ".mp4":
        raise SourceEvidenceError("STEP05_SOURCE_SHA_MISMATCH")
    if item.get("bytes") != path.stat().st_size:
        raise SourceEvidenceError("STEP05_SOURCE_BYTES_MISMATCH")
    # Do not impose >=30 seconds on SOURCE. >=30s applies to produced video.
    from scripts.download_first_source import _probe_audio
    proof = _probe_audio(path)
    if not proof["audio"]:
        raise SourceEvidenceError("STEP05_SOURCE_AUDIO_UNVERIFIED")
    return VerifiedSource(path=path, reel_url=link.video_url,
                          sha256=digest, size=path.stat().st_size)


def validate_caption_evidence(evidence: dict, source: VerifiedSource) -> str:
    if (evidence.get("source_reel_url") != source.reel_url
            or evidence.get("source_sha256") != source.sha256
            or evidence.get("source_page") != SOURCE_CANONICAL):
        raise SourceEvidenceError("STEP05_CAPTION_SOURCE_MISMATCH")
    status = evidence.get("status")
    if status not in {"SOURCE_HASHTAGS_CAPTURED", "SOURCE_CAPTION_NO_HASHTAGS"}:
        raise SourceEvidenceError("STEP05_CAPTION_NOT_VERIFIED")
    caption = evidence.get("caption_text")
    if (not isinstance(caption, str) or not caption.strip()
            or sha256(caption.encode()).hexdigest() != evidence.get("caption_sha256")
            or evidence.get("metadata_source") not in {
               "FACEBOOK_REEL_OG_DESCRIPTION", "FACEBOOK_REEL_META_DESCRIPTION",
               "FACEBOOK_REEL_VISIBLE_CAPTION"}):
        raise SourceEvidenceError("STEP05_CAPTION_SHA_OR_PROVENANCE_FAILED")
    actual = tags_from_caption(caption)
    if status == "SOURCE_CAPTION_NO_HASHTAGS" and actual:
        raise SourceEvidenceError("STEP05_NO_TAGS_FALSE")
    if evidence.get("hashtags") != actual:
        raise SourceEvidenceError("STEP05_HASHTAGS_DONT_MATCH_CAPTION")
    if status == "SOURCE_HASHTAGS_CAPTURED" and not actual:
        raise SourceEvidenceError("STEP05_TAGS_MISSING")
    return " ".join(actual)


class ExistingVerifiedSourceTools:
    """One concrete Tools.collect implementation, not another crawler pipeline.

    The Agent invokes collect on NEW. The capture worker navigates ONLY an
    existing Chrome CDP tab; the archive is verified *before* the Agent Store
    moves the job to SOURCE_READY.
    """
    def __init__(self, media, source: VerifiedSource,
                 caption_path: Path = CAPTION_PATH,
                 worker: Path = CAPTURE_WORKER,
                 archive_receipt=None):
        self.media, self.source = media, source
        self.caption_path, self.worker = Path(caption_path), Path(worker)
        self.archive_receipt = archive_receipt

    def _caption(self) -> str:
        print("TOOL_SYSTEM_STEP05_TASK=READ_EXACT_REEL_HASHTAGS", flush=True)
        if self.caption_path.exists():
            try:
                saved = json.loads(self.caption_path.read_text(encoding="utf-8"))
                return validate_caption_evidence(saved, self.source)
            except (ValueError, OSError, SourceEvidenceError):
                # Invalidate a stale record without deleting it; new version
                # is regenerated atomically by capture subprocess.
                pass
        if not self.worker.is_file():
            raise SourceEvidenceError("STEP05_CAPTION_WORKER_MISSING")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/root"),
            "DISPLAY": ":99",
            "PYTHONPATH": str(self.worker.parent.parent / "src"),
        }
        try:
            r = subprocess.run([sys.executable, str(self.worker),
                                self.source.reel_url, self.source.sha256],
                               env=env, text=True, capture_output=True,
                               timeout=65, check=False)
        except subprocess.TimeoutExpired:
            raise SourceEvidenceError("STEP05_CAPTION_BROWSER_TIMEOUT") from None
        if r.returncode:
            # Never log signed URLs / browser authentication responses.
            raise SourceEvidenceError("STEP05_CAPTION_BROWSER_FAILED:" +
                                      (r.stdout.strip()[-100:] or "WORKER_ERROR"))
        if not self.caption_path.is_file():
            raise SourceEvidenceError("STEP05_CAPTION_REPORT_MISSING")
        return validate_caption_evidence(
            json.loads(self.caption_path.read_text(encoding="utf-8")),
            self.source)

    def collect(self, job: Job) -> SourceResult:
        if job.page != SOURCE_PAGE or job.source_url != self.source.reel_url:
            raise SourceEvidenceError("STEP05_WRONG_JOB_PAGE_OR_REEL")
        # Validate local bytes immediately before cloud upload.
        if digest_file(self.source.path) != self.source.sha256:
            raise SourceEvidenceError("STEP05_SOURCE_CHANGED_BEFORE_UPLOAD")
        # ARCHIVE FIRST. The old order extracted hashtags before Supabase:
        # a slow Facebook UI operation could leave the video unbacked when
        # Kaggle stopped. Read-back SHA is mandatory before caption scraping.
        print("TOOL_SYSTEM_STEP05_TASK=SUPABASE_MP4_UPLOAD_AND_SHA_READBACK", flush=True)
        key = self.media.upload_mp4(job.id, self.source.path, self.source.sha256)
        print("TOOL_SYSTEM_STEP05_TASK=SUPABASE_MP4_CONFIRMED", flush=True)
        if self.archive_receipt is not None:
            # Synchronously persist the media receipt and SQLite checkpoint.
            # An interrupted Facebook caption step must never orphan media.
            self.archive_receipt(job, key)
            print("TOOL_SYSTEM_STEP05_TASK=SOURCE_ARCHIVE_RECEIPT_CHECKPOINTED",
                  flush=True)
        tags = self._caption()
        return SourceResult(path=self.source.path, hashtags=tags)

    def error(self, job: Job, message: str) -> None:
        # The active notebook always exposes the error. If the same worker has
        # configured Telegram keys, also send the owner a real alert.
        print("TOOL_SYSTEM_STEP05_AGENT_ERROR=" + str(message)[:200], flush=True)
        token = os.getenv("TOOL_SYSTEM_TELEGRAM_BOT_TOKEN") or os.getenv("MONEY_TELEGRAM_BOT_TOKEN")
        owner = os.getenv("TOOL_SYSTEM_TELEGRAM_OWNER_CHAT_ID") or os.getenv("MONEY_TELEGRAM_OWNER_CHAT_ID")
        if token and owner:
            from .telegram import Telegram
            Telegram(token, int(owner), None).error(job, message)
        else:
            # Prevent fake claim that Telegram was informed.
            print("TOOL_SYSTEM_TELEGRAM_ALERT=NOT_CONFIGURED", flush=True)
