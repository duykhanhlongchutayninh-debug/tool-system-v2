"""Recover first Facebook source ONCE and archive MP4 BEFORE any caption/Gemini.

Use only from the owner's existing Kaggle CPU Notebook AFTER:
- GitHub auto-updater current release is restored,
- exactly one visible Chrome is running at local CDP 9222,
- Tool System V2's NEW Supabase secret is active in the Notebook kernel.

Never touch the old repository, create a second browser, or rerun Chrome setup.
If a verified local MP4 exists, skip Facebook entirely. If not, run the
previously proven V2-style source crawler in an isolated child process.

On the first completed MP4, synchronously store it in private Supabase
with full SHA-256 read-back BEFORE extracting hashtags or calling Gemini.
No false success and no retry loop. Existing jobs are idempotent.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from money_ai.cloud_checkpoint import CloudCheckpoint
from money_ai.cloud_media import CloudMedia
from money_ai.durable_store import DurableStore
from money_ai.source_step05 import (
    SOURCE_PAGE, SOURCE_APPROVED, SOURCE_CANONICAL, REPORT_PATH,
    VerifiedSource, verified_local_source, SourceEvidenceError
)
from money_ai.facebook_video_urls import classify_video_link

STORE_PATH = Path("/kaggle/working/tool-system-v2-runtime/state/jobs.sqlite")
LEGACY = ROOT / "scripts" / "source_legacy_verified.py"
TIMEOUT = 360


def _require_live_supabase() -> CloudCheckpoint:
    """Fail before an expensive crawl if this NEW session cannot archive."""
    if not os.environ.get("TOOL_SYSTEM_SUPABASE_SERVICE_ROLE_KEY", "").strip():
        raise RuntimeError("SUPABASE_KEY_NOT_ACTIVE_IN_NEW_KAGGLE_SESSION")
    checkpoint = CloudCheckpoint()
    checkpoint.latest()  # validates correct project + endpoint + secret
    return checkpoint


def _get_existing_source():
    try:
        source = verified_local_source()
    except (SourceEvidenceError, FileNotFoundError, OSError, ValueError):
        return None
    print("TOOL_SYSTEM_RECOVERY=REUSE_VERIFIED_LOCAL_MP4", flush=True)
    return source



def _restore_source_from_supabase(checkpoint) -> VerifiedSource | None:
    """Recover a previously archived MP4 from durable receipt without Chrome.

    A source SHA/object key stored in a backed-up SQLite event makes the
    media retrievable after Kaggle destroys /working. This must happen BEFORE
    any new crawl; if an archive exists but cannot be restored, do not
    substitute an arbitrary source video.
    """
    store = DurableStore(STORE_PATH, checkpoint)
    try:
        jobs = list(reversed(store.list_jobs()))
        media = CloudMedia()
        for job in jobs:
            if job.page != SOURCE_PAGE or classify_video_link(job.source_url).kind != "REEL_POST":
                continue
            receipt = None
            for event in reversed(store.events(job.id)):
                if event.get("kind") != "SOURCE_MP4_ARCHIVED":
                    continue
                try:
                    data = json.loads(event.get("details") or "")
                except (ValueError, TypeError):
                    continue
                if data.get("source_url") == job.source_url:
                    receipt = data
                    break
            if receipt is None and job.source_sha:
                # Older SOURCE_READY jobs may have media but no new event.
                receipt = {
                    "source_url": job.source_url,
                    "sha256": job.source_sha,
                    "remote_key": f"videos/{job.id}/{job.source_sha}.mp4",
                }
            if receipt is None:
                continue
            digest = receipt.get("sha256")
            key = receipt.get("remote_key")
            if (not isinstance(digest, str) or len(digest) != 64 or
                    key != f"videos/{job.id}/{digest}.mp4"):
                raise RuntimeError("SOURCE_BACKUP_RECEIPT_INVALID")
            dest = Path("/kaggle/working/tool-system-v2-runtime/sources") / (digest + ".mp4")
            # CloudMedia.restore_mp4 validates byte count and SHA-256.
            media.restore_mp4(key, digest, dest)
            # Restore the local source manifest solely from trusted job+receipt.
            manifest = {
                "source_page": SOURCE_CANONICAL,
                "approved_share_url": SOURCE_APPROVED,
                "source_reel": job.source_url,
                "status": "REAL_MP4_WITH_AUDIO_VERIFIED",
                "earliest_video_verified": False,
                "selected": {
                    "mp4_path": str(dest),
                    "sha256": digest, "bytes": dest.stat().st_size,
                    "audio": True, "source_reel_url": job.source_url,
                }
            }
            REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
            temp = REPORT_PATH.with_suffix(".restore-tmp")
            temp.write_text(json.dumps(manifest, ensure_ascii=False),
                            encoding="utf-8")
            temp.replace(REPORT_PATH)
            # Re-check local file/audio rather than trusting manifest alone.
            verified = verified_local_source()
            print("TOOL_SYSTEM_RECOVERY=RESTORED_VERIFIED_MP4_FROM_SUPABASE",
                  flush=True)
            return verified
        return None
    finally:
        store.close()


def _check_existing_chrome():
    try:
        with socket.create_connection(("127.0.0.1", 9222), timeout=3):
            return
    except OSError:
        raise RuntimeError(
            "ONE_CHROME_CDP_NOT_RUNNING: restore previous Kaggle Chrome first"
        ) from None


def _show_safe_crawler_diagnostics(report_path: Path = REPORT_PATH) -> None:
    """Report saved failure *categories* without URLs, keys or media signatures."""
    if not report_path.is_file():
        print("TOOL_SYSTEM_CRAWLER_REPORT=NOT_FOUND", flush=True)
        return
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("TOOL_SYSTEM_CRAWLER_REPORT=INVALID", flush=True)
        return

    import re
    def safe(value) -> str:
        value = str(value or "")
        return value if re.fullmatch(r"[A-Z][A-Z0-9_]{2,90}", value) else "UNKNOWN"

    print("TOOL_SYSTEM_CRAWLER_RESULT=" +
          safe(report.get("status")), flush=True)
    grid = report.get("grid_evidence") or {}
    print("TOOL_SYSTEM_CRAWLER_GRID=" +
          safe(grid.get("status")), flush=True)
    n = grid.get("loaded_reel_count", 0)
    if type(n) is int:
        print("TOOL_SYSTEM_CRAWLER_REELS_LOADED=" +
              str(min(max(n, 0), 10000)), flush=True)
    for index, reason in enumerate(report.get("attempt_reasons") or [], 1):
        if index > 12:
            break
        print(f"TOOL_SYSTEM_CRAWLER_ATTEMPT_{index}=" +
              safe(reason), flush=True)


def _run_proven_source_recovery():
    """The working crawler is unchanged; its subprocess never sees secrets."""
    _check_existing_chrome()
    if not LEGACY.is_file():
        raise RuntimeError("PROVEN_SOURCE_CRAWLER_MISSING")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/root"),
        "DISPLAY": ":99",
        "PYTHONPATH": str(ROOT / "src"),
    }
    print("TOOL_SYSTEM_RECOVERY=CRAWL_KNOWN_PAGE_MAX_2_REELS", flush=True)
    print("TOOL_SYSTEM_RECOVERY_NOTICE=ARCHIVE_IMMEDIATELY_AFTER_AV_PROOF", flush=True)
    try:
        result = subprocess.run(
            [sys.executable, str(LEGACY), "--worker"],
            env=env, capture_output=True, text=True, timeout=TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("VERIFIED_CRAWLER_TIMEOUT") from None
    if (result.returncode
            or "TOOL_SYSTEM_LEGACY_PATTERN_RESULT=REAL_MP4_WITH_AUDIO_VERIFIED"
            not in result.stdout):
        # Do not leak CDN URLs or huge tracebacks from child stdout/stderr.
        _show_safe_crawler_diagnostics()
        safe = ("CRAWLER_EXIT_" + str(result.returncode)
                if result.returncode else "CRAWLER_HAS_NO_AV_SUCCESS_PROOF")
        raise RuntimeError(safe)
    # Do not print the child's secrets or signed media URLs; read the
    # well-defined local report and verify the actual bytes instead.
    return verified_local_source()


def archive_first(*, checkpoint=None, store_type=DurableStore,
                  media_type=CloudMedia, source_loader=_get_existing_source,
                  restorer=_restore_source_from_supabase,
                  crawler=_run_proven_source_recovery) -> dict:
    """Verify remote media BEFORE doing anything expensive with captions.

    Dependency injection is only for deterministic unit tests. In production
    cloud checkpoint is always live and read back, not merely HTTP accepted.
    """
    checkpoint = checkpoint or _require_live_supabase()
    print("TOOL_SYSTEM_RECOVERY=SUPABASE_CONNECTED", flush=True)
    source = source_loader()
    if source is None:
        print("TOOL_SYSTEM_RECOVERY=CHECK_PREVIOUS_SUPABASE_MEDIA", flush=True)
        source = restorer(checkpoint)
    if source is None:
        source = crawler()
    print("TOOL_SYSTEM_RECOVERY=LOCAL_MP4_AUDIO_SHA_VERIFIED", flush=True)
    store = store_type(STORE_PATH, checkpoint)
    try:
        job = store.create(SOURCE_PAGE, source.reel_url)
        reconcile_blocked_source_ready = False
        reuse_advanced_job = False
        # An idempotent create() may return the SAME job that already reached
        # Gemini/script/video work. Step05 must not roll that job backwards or
        # reject a source whose SHA is still identical and verified. It only
        # refreshes the immutable archive receipt and leaves the later stage
        # untouched. A changed source SHA is never merged into an old job.
        later_stages = {
            "SCRIPT_READY", "VIDEO_READY", "WAITING_APPROVAL",
            "REVISION_REQUESTED", "APPROVED", "PUBLISHING", "PUBLISHED",
        }
        if job.stage == "BLOCKED":
            if job.resume_stage == "NEW":
                job = store.resume(job.id)
                print("TOOL_SYSTEM_RECOVERY=RESUMED_SAFE_SOURCE_JOB", flush=True)
            elif job.resume_stage == "SOURCE_READY":
                if job.source_sha != source.sha256:
                    raise RuntimeError("STEP05_BLOCKED_JOB_CANNOT_AUTO_RESUME")
                reconcile_blocked_source_ready = True
                print("TOOL_SYSTEM_RECOVERY=RECONCILE_BLOCKED_SOURCE_READY", flush=True)
            elif job.resume_stage in later_stages:
                if job.source_sha != source.sha256:
                    raise RuntimeError("STEP05_BLOCKED_JOB_SOURCE_SHA_MISMATCH")
                reuse_advanced_job = True
                print("TOOL_SYSTEM_RECOVERY=REUSE_EXISTING_ADVANCED_JOB", flush=True)
            else:
                raise RuntimeError("STEP05_BLOCKED_JOB_CANNOT_AUTO_RESUME")
        elif job.stage in later_stages:
            if job.source_sha != source.sha256:
                raise RuntimeError("STEP05_EXISTING_JOB_SOURCE_SHA_MISMATCH")
            reuse_advanced_job = True
            print("TOOL_SYSTEM_RECOVERY=REUSE_EXISTING_ADVANCED_JOB", flush=True)
        elif job.stage == "SOURCE_READY" and job.source_sha != source.sha256:
            raise RuntimeError("STEP05_EXISTING_SOURCE_SHA_MISMATCH")
        print("TOOL_SYSTEM_RECOVERY=SOURCE_JOB_CHECKPOINT_SAVED", flush=True)
        # CloudMedia.upload_mp4 performs POST + GET and full byte/SHA match.
        # The existing immutable key is okay and is verified by the same GET.
        remote_key = media_type().upload_mp4(
            job.id, source.path, source.sha256)
        # A remote MP4 alone is not enough: a restarted Kaggle must know
        # the exact source SHA/object key. Backup a durable SQLite receipt now.
        store.record_source_archive(job.id, source.sha256, remote_key)
        print("TOOL_SYSTEM_SOURCE_BACKUP=VERIFIED", flush=True)
        print("TOOL_SYSTEM_SOURCE_BACKUP_SHA256=" + source.sha256, flush=True)
        print("TOOL_SYSTEM_SOURCE_BACKUP_REMOTE_KEY=" + remote_key, flush=True)
        if reconcile_blocked_source_ready:
            # Re-open the blocked source task only AFTER remote upload/readback
            # SHA verification and durable archive-receipt checkpoint succeeded.
            # Rebind the path to this Kaggle session's restored MP4; don't use
            # a stale /working path from the previous session.
            job = store.move(
                job.id, "SOURCE_READY",
                note="Recovered archived source MP4; SHA reverified in current session",
                source_path=str(source.path), source_sha=source.sha256,
                hashtags="", error=None, resume_stage=None,
            )
            print("TOOL_SYSTEM_RECOVERY=BLOCKED_SOURCE_READY_RECONCILED", flush=True)
        elif reuse_advanced_job:
            print("TOOL_SYSTEM_RECOVERY=ADVANCED_JOB_STAGE_PRESERVED", flush=True)
        print("TOOL_SYSTEM_STEP05_JOB=" + job.id, flush=True)
        print("TOOL_SYSTEM_AGENT_STAGE=" + job.stage, flush=True)
        print("TOOL_SYSTEM_STEP06=NOT_STARTED", flush=True)
        return {"status": "BACKUP_VERIFIED", "job_id": job.id,
                "sha256": source.sha256, "source_url": source.reel_url,
                "remote_key": remote_key, "stage": job.stage,
                "resume_stage": getattr(job, "resume_stage", None)}
    finally:
        store.close()


if __name__ == "__main__":
    print("TOOL_SYSTEM_RECOVERY_BEGIN", flush=True)
    try:
        _ = archive_first()
    except Exception as exc:
        print("TOOL_SYSTEM_SOURCE_BACKUP=NOT_VERIFIED", flush=True)
        # Stable error codes, never key/HTTP response body/CDN url.
        print("TOOL_SYSTEM_RECOVERY_ERROR=" + type(exc).__name__ +
              ":" + str(exc)[:130], flush=True)
        raise
