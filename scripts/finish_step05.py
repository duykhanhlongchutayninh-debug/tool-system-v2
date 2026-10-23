"""Step05 source-only checkpoint: MP4/audio/SHA + Supabase receipt -> SOURCE_READY.

No captions, hashtags, extra CloudMedia.upload_mp4 or browser tab. Archive
must have completed successfully in recover_source_archive_first beforehand.
Uses the real Store integrity gate and durable cloud checkpoint for the job.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for part in (ROOT, ROOT / "src"):
    if str(part) not in sys.path:
        sys.path.insert(0, str(part))

from money_ai.cloud_checkpoint import CloudCheckpoint
from money_ai.durable_store import DurableStore
from money_ai.source_step05 import SOURCE_PAGE, verified_local_source

STORE_PATH = Path("/kaggle/working/tool-system-v2-runtime/state/jobs.sqlite")


def run(receipt: dict | None = None) -> dict:
    """Only the real verified MP4 source is needed; skip captions and hashtags."""
    print("TOOL_SYSTEM_STEP05_PROGRESS=1/3_VERIFY_MP4", flush=True)
    source = verified_local_source()
    if not os.environ.get("TOOL_SYSTEM_SUPABASE_SERVICE_ROLE_KEY"):
        raise RuntimeError("STEP05_SUPABASE_KEY_NOT_ACTIVE")
    if not isinstance(receipt, dict) or receipt.get("status") != "BACKUP_VERIFIED":
        raise RuntimeError("STEP05_CLOUD_RECEIPT_REQUIRED")
    if receipt.get("sha256") != source.sha256:
        raise RuntimeError("STEP05_SOURCE_RECEIPT_SHA_MISMATCH")
    checkpoint = CloudCheckpoint()
    store = DurableStore(STORE_PATH, checkpoint)
    try:
        job = store.get(receipt["job_id"])
        if job.page != SOURCE_PAGE or job.source_url != source.reel_url:
            raise RuntimeError("STEP05_SOURCE_JOB_MISMATCH")
        later_stages = {
            "SCRIPT_READY", "VIDEO_READY", "WAITING_APPROVAL",
            "REVISION_REQUESTED", "APPROVED", "PUBLISHING", "PUBLISHED",
        }

        def verified_source_result(current_job, *, reused=False):
            if current_job.source_sha != source.sha256:
                raise RuntimeError("STEP05_PREVIOUS_SOURCE_MISMATCH")
            print("TOOL_SYSTEM_STEP05_PROGRESS=3/3_ALL_PROOFS_VERIFIED", flush=True)
            if reused:
                print("TOOL_SYSTEM_STEP05_SOURCE_CHECKPOINT_REUSED=PASS", flush=True)
                print("TOOL_SYSTEM_STEP05_EXISTING_STAGE=" + current_job.stage, flush=True)
            else:
                print("TOOL_SYSTEM_STEP05=SOURCE_READY_VERIFIED", flush=True)
            print("TOOL_SYSTEM_STEP05_CAPTION=SKIPPED_BY_OWNER", flush=True)
            print("TOOL_SYSTEM_STEP05_HASHTAGS=SKIPPED_BY_OWNER", flush=True)
            print("TOOL_SYSTEM_STEP06=NOT_STARTED", flush=True)
            return {
                "result": "SOURCE_READY_VERIFIED",
                "job_id": current_job.id,
                "source_sha256": current_job.source_sha,
                "source_url": current_job.source_url,
                "stage": current_job.stage,
                "source_checkpoint_reused": reused,
            }

        if job.stage == "BLOCKED":
            if job.resume_stage == "NEW":
                # A first attempt may have been blocked before attaching the
                # verified MP4. Its source_sha is legitimately still empty.
                job = store.resume(job.id)
            elif job.source_sha != source.sha256:
                raise RuntimeError("STEP05_BLOCKED_JOB_SOURCE_SHA_MISMATCH")
            elif job.resume_stage == "SOURCE_READY":
                # Recovery already verified the remote archive. Repair the path
                # against this session's exact MP4 SHA, then restore SOURCE_READY.
                job = store.move(
                    job.id, "SOURCE_READY",
                    note="Rebind verified source after Kaggle session recovery",
                    source_path=str(source.path), source_sha=source.sha256,
                    hashtags="", error=None, resume_stage=None,
                )
            elif job.resume_stage in later_stages:
                # Do not roll an existing script/video/approval task backwards.
                # Step05 only revalidates its immutable source checkpoint.
                return verified_source_result(job, reused=True)
            else:
                raise RuntimeError("STEP05_BLOCKED_UNSAFE")
        if job.stage == "NEW":
            print("TOOL_SYSTEM_STEP05_PROGRESS=2/3_AGENT_SOURCE_READY", flush=True)
            job = store.move(job.id, "SOURCE_READY",
                             note="Verified source MP4 and audio, captions not required",
                             source_path=str(source.path),
                             source_sha=source.sha256, hashtags="")
        elif job.stage == "SOURCE_READY":
            if job.source_sha != source.sha256:
                raise RuntimeError("STEP05_PREVIOUS_SOURCE_MISMATCH")
            if job.source_path != str(source.path):
                # Same source bytes, new Kaggle /working path after session reset.
                # Persist this narrowly verified path repair before advancing.
                job = store.rebind_verified_source_path(
                    job.id, source.path, source.sha256)
        elif job.stage in later_stages:
            # Same immutable source, later business stage already exists.
            # This is an idempotent Step05 pass; preserve the later stage.
            return verified_source_result(job, reused=True)
        else:
            raise RuntimeError("STEP05_ALREADY_AT_STAGE_" + job.stage)
        if job.stage != "SOURCE_READY":
            raise RuntimeError("STEP05_SOURCE_NOT_READY")
        return verified_source_result(job, reused=False)
    finally:
        store.close()


if __name__ == "__main__":
    try:
        _ = run()
    except Exception as exc:
        # Do not print underlying sensitive CDN URLs or provider responses.
        print("TOOL_SYSTEM_STEP05=NOT_VERIFIED", flush=True)
        print("TOOL_SYSTEM_STEP05_ERROR=" + type(exc).__name__ +
              ":" + str(exc)[:180], flush=True)
        raise
