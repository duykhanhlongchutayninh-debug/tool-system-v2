"""Tool System V2 BUSINESS Step 05: one real Facebook source to durable SOURCE_READY.

Starts ONLY after the owner invokes the existing Kaggle Notebook business
button. Uses the SAME Chrome CDP 9222; never launches a new browser, copies
any credentials, sends a Gemini prompt, or publishes to Facebook.

One run:
  1. Reuse verified local source MP4, or recover by Supabase media receipt,
     or (only if neither exists) crawl the approved first source.
  2. Upload/read back the real MP4 to the existing private Supabase media bucket.
  3. Skip source captions/hashtags entirely as directed by the owner.
  4. Move the verified MP4 job to SOURCE_READY with a cloud checkpoint.

Any stage failure is fatal to this run; a second Page is NOT automatically
started and Gemini NEVER runs on missing/unsupported evidence.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
from pathlib import Path
import socket
import sys
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
for part in (ROOT, ROOT / "src"):
    if str(part) not in sys.path:
        sys.path.insert(0, str(part))

LOCK = Path("/kaggle/working/tool-system-v2-runtime/state/business-step05.lock")


def _mark_active_business_job_unverified() -> None:
    """Prevent Step06/07 falling back to a stale job when this Step05 run fails."""
    path = LOCK.parent / "active_business_job.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "STEP05_IN_PROGRESS",
        "evidence": "STEP05_SOURCE_NOT_YET_VERIFIED",
    }
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)
    os.chmod(path, 0o600)


def _record_active_business_job(receipt: dict, *, stage: str = "SOURCE_READY") -> None:
    """Bind Step06/Step07 to the exact Step05 job, even if a later gate fails."""
    job_id = receipt.get("job_id")
    digest = receipt.get("sha256") or receipt.get("source_sha256")
    source_url = receipt.get("source_url", "")
    if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{16}", job_id):
        raise RuntimeError("STEP05_ACTIVE_JOB_ID_INVALID")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise RuntimeError("STEP05_ACTIVE_JOB_SHA_INVALID")
    path = LOCK.parent / "active_business_job.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "job_id": job_id,
        "source_sha256": digest,
        "source_url": str(source_url),
        "source_stage": str(stage),
        "valid": True,
        "evidence": "STEP05_MP4_SHA_AND_SUPABASE_RECEIPT_VERIFIED",
    }
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)
    os.chmod(path, 0o600)
    print("TOOL_SYSTEM_STEP05_ACTIVE_JOB=RECORDED", flush=True)
    print("TOOL_SYSTEM_STEP05_ACTIVE_JOB_ID=" + job_id, flush=True)


def _verify_kaggle_business_preflight() -> None:
    if not Path("/kaggle").is_dir():
        raise RuntimeError("STEP05_REQUIRES_EXISTING_KAGGLE_CPU")
    if not os.getenv("TOOL_SYSTEM_SUPABASE_SERVICE_ROLE_KEY"):
        raise RuntimeError("STEP05_EXISTING_SUPABASE_KEY_NOT_ACTIVE")
    try:
        with socket.create_connection(("127.0.0.1", 9222), timeout=3):
            pass
    except OSError:
        raise RuntimeError("STEP05_EXISTING_CHROME_CDP_9222_OFFLINE") from None
    if os.environ.get("TOOL_SYSTEM_CDP_URL") not in (None, "",
                                                    "http://127.0.0.1:9222"):
        raise RuntimeError("STEP05_UNEXPECTED_CHROME_ENDPOINT")
    os.environ["TOOL_SYSTEM_CDP_URL"] = "http://127.0.0.1:9222"
    os.environ["DISPLAY"] = ":99"


def execute(*, recover: Callable | None = None,
            finish: Callable | None = None,
            preflight: Callable = _verify_kaggle_business_preflight) -> dict:
    """One locked, evidence-gated production run; injection only for tests."""
    preflight()
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("STEP05_ALREADY_RUNNING") from None

        if recover is None:
            from scripts.recover_source_archive_first import archive_first
            recover = archive_first
        if finish is None:
            from scripts.finish_step05 import run
            finish = run

        print("TOOL_SYSTEM_BUSINESS_STEP05=STARTED", flush=True)
        _mark_active_business_job_unverified()
        print("TOOL_SYSTEM_BUSINESS_STEP05_PHASE=RECOVER_OR_CRAWL_ARCHIVE", flush=True)
        receipt = recover()
        if (not isinstance(receipt, dict)
                or receipt.get("status") != "BACKUP_VERIFIED"
                or not receipt.get("job_id")
                or not isinstance(receipt.get("sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", receipt["sha256"])):
            raise RuntimeError("STEP05_SOURCE_CLOUD_ARCHIVE_NOT_VERIFIED")
        # Persist the exact job binding immediately after remote MP4 readback.
        # Step06 must not guess among old jobs if the later Step05 gate fails.
        _record_active_business_job(receipt, stage=str(receipt.get("stage", "SOURCE_READY")))
        print("TOOL_SYSTEM_BUSINESS_STEP05_PHASE=SOURCE_MP4_CLOUD_VERIFIED", flush=True)
        print("TOOL_SYSTEM_BUSINESS_STEP05_PHASE=SOURCE_ONLY_AGENT_CHECKPOINT", flush=True)
        result = finish(receipt)
        if (not isinstance(result, dict)
                or result.get("result") != "SOURCE_READY_VERIFIED"
                or result.get("job_id") != receipt["job_id"]
                or result.get("source_sha256") != receipt["sha256"]):
            raise RuntimeError("STEP05_AGENT_OR_SOURCE_HASH_NOT_VERIFIED")
        _record_active_business_job({
            "job_id": result["job_id"],
            "source_sha256": result["source_sha256"],
            "source_url": result.get("source_url") or receipt.get("source_url", ""),
        }, stage=str(result.get("stage", "SOURCE_READY")))
        print("TOOL_SYSTEM_BUSINESS_STEP05=SOURCE_READY_VERIFIED", flush=True)
        print("TOOL_SYSTEM_BUSINESS_NEXT_GATE=GEMINI_MP4_VIEW_AND_LISTEN", flush=True)
        return result


def main() -> None:
    try:
        result = execute()
    except Exception as exc:
        # Do not dump exception contents: provider URLs and signed CDNs may
        # appear in an untrusted exception. Subtasks already print safe codes.
        print("TOOL_SYSTEM_BUSINESS_STEP05=BLOCKED", flush=True)
        detail = str(exc).strip().split(":", 1)[0]
        # Only emit machine-readable, secret-free error codes. Real browser
        # exceptions may embed private Reel media URLs or login details.
        reason = (detail if re.fullmatch(r"[A-Z][A-Z0-9_]{3,99}", detail)
                  else type(exc).__name__)
        print("TOOL_SYSTEM_BUSINESS_STEP05_ERROR_CODE=" + reason, flush=True)
        print("TOOL_SYSTEM_BUSINESS_STEP05_FAILURE_CLASS=" +
              type(exc).__name__, flush=True)
        # No automatic step06, retries, or real Facebook publishing.
        raise SystemExit(1) from None
    print("TOOL_SYSTEM_BUSINESS_STEP05_JOB=" + result["job_id"], flush=True)


if __name__ == "__main__":
    main()
