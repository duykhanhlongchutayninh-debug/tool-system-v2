"""Business Step07 — generate/verify MP4, archive it, and request Telegram approval.

Run after Step06 has verified SCRIPT_READY. Uses the existing CDP 9222 Chrome
session and current Supabase/Telegram configuration. Never publishes to Facebook.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for part in (ROOT, ROOT / "src"):
    if str(part) not in sys.path:
        sys.path.insert(0, str(part))

from money_ai.agent import Agent
from money_ai.cloud_checkpoint import CloudCheckpoint
from money_ai.durable_store import DurableStore
from money_ai.production_tools import ProductionTools

DB = Path("/kaggle/working/tool-system-v2-runtime/state/jobs.sqlite")
# Share the Step06 lock: both stages mutate the same durable job and Chrome session.
LOCK = Path("/kaggle/working/tool-system-v2-runtime/state/business-step06.lock")
PAGE = "Mẹo nhỏ mỗi ngày"
_JOB_ID = re.compile(r"[a-f0-9]{16}\Z")
# Retry only failures proven to occur before any external side effect. In
# particular, never retry uncertain browser generation or Telegram sends.
_SAFE_PREFLIGHT_ERRORS = {
    "SCRIPT_READY": {
        "VIDEO_GENERATION_REQUIRES_SCRIPT_READY",
        "VIDEO_SCRIPT_SHA_MISMATCH",
        "VIDEO_PROVIDER_MUST_BE_FLOW_OR_META_AI",
    },
    "VIDEO_READY": {
        "TELEGRAM_OWNER_CONFIGURATION_MISSING",
        "TELEGRAM_REVIEW_REQUIRES_VERIFIED_VIDEO",
        "TELEGRAM_REVIEW_VIDEO_SHA_MISMATCH",
    },
}


def _blocked_reason_code(job) -> str:
    """Extract only an uppercase machine code; never echo raw provider/URL text."""
    raw = str(getattr(job, "error", "") or "")
    candidates = re.findall(r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9_]{3,90})(?![A-Za-z0-9_])", raw)
    known = {code for codes in _SAFE_PREFLIGHT_ERRORS.values() for code in codes}
    for code in candidates:
        if code in known:
            return code
    return candidates[0] if candidates else "UNCLASSIFIED_PRIOR_FAILURE"


def _safe_blocked_retry(job) -> bool:
    if job.stage != "BLOCKED" or job.resume_stage not in _SAFE_PREFLIGHT_ERRORS:
        return False
    # Store implementations may wrap a safe machine code in exception text.
    # Retry remains restricted to the explicit preflight allowlist.
    return _blocked_reason_code(job) in _SAFE_PREFLIGHT_ERRORS[job.resume_stage]


def _step07_video_env(env=None) -> dict[str, str]:
    """B07 uses the owner's already signed-in Meta AI web tool, not the Flow default."""
    selected = dict(os.environ if env is None else env)
    selected["TOOL_SYSTEM_VIDEO_PROVIDER"] = "meta"
    return selected


def execute(*, job_id: str | None = None, resume_blocked: bool = False) -> dict:
    if not os.environ.get("TOOL_SYSTEM_SUPABASE_SERVICE_ROLE_KEY"):
        raise RuntimeError("STEP07_SUPABASE_CHECKPOINT_KEY_MISSING")
    if job_id is not None and not _JOB_ID.fullmatch(job_id):
        raise RuntimeError("STEP07_INVALID_JOB_ID")
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("STEP07_BUSINESS_WORKFLOW_ALREADY_RUNNING") from None
        os.environ["DISPLAY"] = ":99"
        os.environ["TOOL_SYSTEM_CDP_URL"] = "http://127.0.0.1:9222"
        store = DurableStore(DB, CloudCheckpoint())
        try:
            if job_id is None:
                # Reuse the exact SHA-bound job selected by Step05, rather than
                # searching all historical jobs and risking a wrong Page/task.
                active_job_path = Path(DB).parent / "active_business_job.json"
                if active_job_path.is_file():
                    try:
                        active = json.loads(active_job_path.read_text(encoding="utf-8"))
                        active_id = active.get("job_id")
                        active_sha = active.get("source_sha256")
                    except (OSError, ValueError, TypeError):
                        raise RuntimeError("STEP07_ACTIVE_JOB_CHECKPOINT_INVALID") from None
                    if (active.get("evidence") != "STEP05_MP4_SHA_AND_SUPABASE_RECEIPT_VERIFIED"
                            or active.get("valid") is not True
                            or not isinstance(active_id, str) or not _JOB_ID.fullmatch(active_id)
                            or not isinstance(active_sha, str)
                            or not re.fullmatch(r"[a-f0-9]{64}", active_sha)):
                        raise RuntimeError("STEP07_ACTIVE_JOB_CHECKPOINT_INVALID")
                    bound_job = store.get(active_id)
                    if (bound_job.page != PAGE or bound_job.source_sha != active_sha
                            or (active.get("source_url")
                                and bound_job.source_url != active["source_url"])):
                        raise RuntimeError("STEP07_ACTIVE_JOB_SOURCE_MISMATCH")
                    job_id = bound_job.id
                    print("TOOL_SYSTEM_STEP07_ACTIVE_JOB=SHA_BOUND_STEP05_CHECKPOINT", flush=True)
                else:
                    ready = [item for item in store.list_jobs()
                             if item.page == PAGE and (
                                 item.stage in ("SCRIPT_READY", "VIDEO_READY", "WAITING_APPROVAL")
                                 or (resume_blocked and _safe_blocked_retry(item)))]
                    if len(ready) != 1:
                        raise RuntimeError("STEP07_JOB_SELECTION_REQUIRED")
                    job_id = ready[0].id
            job = store.get(job_id)
            if job.page != PAGE:
                raise RuntimeError("STEP07_UNEXPECTED_PAGE")
            recovered_video = False
            if job.stage == "BLOCKED":
                if not resume_blocked:
                    raise RuntimeError("STEP07_BLOCKED_JOB_REQUIRES_EXPLICIT_SAFE_RESUME")
                if not _safe_blocked_retry(job):
                    print("TOOL_SYSTEM_STEP07_BLOCKED_RESUME_STAGE=" +
                          str(job.resume_stage or "UNKNOWN"), flush=True)
                    print("TOOL_SYSTEM_STEP07_PRIOR_FAILURE_CODE=" +
                          _blocked_reason_code(job), flush=True)
                    print("TOOL_SYSTEM_STEP07_RECONCILIATION_REQUIRED=TRUE", flush=True)
                    if job.resume_stage != "SCRIPT_READY":
                        raise RuntimeError("STEP07_BLOCKED_STAGE_NOT_SAFE_TO_RETRY")
                    # Inspect the exact job/SHA on Meta AI /create before resuming.
                    # Reconcile first; never retry blindly.
                    from money_ai.meta_ai_web import inspect_prior_generation
                    reconciliation = inspect_prior_generation(job)
                    print("TOOL_SYSTEM_STEP07_RECONCILIATION_STATE=" +
                          reconciliation.state, flush=True)
                    if reconciliation.state == "VIDEO_RECOVERED" and reconciliation.artifact:
                        job = store.resume(job.id)
                        tools = ProductionTools(store, env=_step07_video_env())
                        video_result = tools.verify_and_archive_reconciled_video(
                            job, reconciliation.artifact.path, provider="Meta AI"
                        )
                        from money_ai.core import digest_file
                        job = store.move(
                            job.id, "VIDEO_READY",
                            note="Recovered the existing Meta AI chat artifact by matching the exact script SHA",
                            video_path=str(video_result.path),
                            video_sha=digest_file(video_result.path),
                            duration=video_result.duration,
                            frame_w=video_result.width,
                            frame_h=video_result.height,
                            provider=video_result.provider,
                        )
                        recovered_video = True
                        print("TOOL_SYSTEM_STEP07_RECONCILIATION=PRIOR_VIDEO_RECOVERED_AND_VERIFIED",
                              flush=True)
                    elif reconciliation.state == "UNSUBMITTED_COMPOSER":
                        # The exact script SHA is still in the live composer and
                        # absent from submitted history: submitting that existing
                        # prompt once is the only safe continuation.
                        job = store.resume(job.id)
                        print("TOOL_SYSTEM_STEP07_RECONCILIATION=UNSUBMITTED_PROMPT_VERIFIED",
                              flush=True)
                    elif reconciliation.state == "SUBMITTED_NO_ASSOCIATED_VIDEO":
                        raise RuntimeError("METAAI_PRIOR_REQUEST_OUTCOME_UNKNOWN_NO_RESUBMIT")
                    elif reconciliation.state in {
                            "NO_MATCHING_PRIOR_PROMPT_ON_META_CREATE",
                            "NO_MATCHING_PRIOR_PROMPT",
                    }:
                        # No prior attempt with this exact job/SHA exists on /create.
                        # Continue the same job through the verified composer, once.
                        job = store.resume(job.id)
                        print("TOOL_SYSTEM_STEP07_RECONCILIATION=META_CREATE_NO_PRIOR_PROMPT",
                              flush=True)
                    else:
                        raise RuntimeError("METAAI_PRIOR_PROMPT_NOT_VISIBLE_REQUIRES_OWNER_CHECK")
                else:
                    job = store.resume(job.id)
                    print("TOOL_SYSTEM_STEP07_RESUMED_STAGE=" + job.stage, flush=True)
            if job.stage not in ("SCRIPT_READY", "VIDEO_READY", "WAITING_APPROVAL"):
                raise RuntimeError("STEP07_SCRIPT_OR_VIDEO_READY_REQUIRED")
            if job.stage == "WAITING_APPROVAL":
                if not job.review_message_id:
                    raise RuntimeError("STEP07_TELEGRAM_RECEIPT_MISSING")
                # This is a verified resume, not a no-op failure: Kaggle's
                # existing button uses this marker to restart the Telegram
                # approval poller after a kernel/session restart.
                print("TOOL_SYSTEM_STEP07_STAGE_VERIFIED=WAITING_APPROVAL", flush=True)
                return {"job_id": job.id, "stage": job.stage,
                        "review_message_id": job.review_message_id}
            print("TOOL_SYSTEM_STEP07_SELECTED_JOB=" + job.id, flush=True)
            print("TOOL_SYSTEM_STEP07_PHASE=" + job.stage, flush=True)
            video_env = _step07_video_env()
            print("TOOL_SYSTEM_STEP07_VIDEO_PROVIDER=Meta AI", flush=True)
            agent = Agent.production(store, ProductionTools(store, env=video_env))
            outcome = agent.step(job.id)
            # Agent performs one durable transition per call. A newly generated
            # video needs a second pass to send the exact verified MP4 for review.
            if outcome.stage == "VIDEO_READY":
                outcome = agent.step(job.id)
            if outcome.stage not in ("VIDEO_READY", "WAITING_APPROVAL"):
                if outcome.stage == "BLOCKED":
                    # Agent records the concrete provider/browser failure on the
                    # durable Job. Do not hide that evidence behind this wrapper's
                    # generic "video or review not verified" error.
                    failure_code = _blocked_reason_code(outcome)
                    print("TOOL_SYSTEM_STEP07_AGENT_BLOCKED_REASON_CODE=" +
                          failure_code, flush=True)
                    raise RuntimeError("STEP07_AGENT_BLOCKED_" + failure_code)
                raise RuntimeError("STEP07_VIDEO_OR_REVIEW_NOT_VERIFIED")
            if outcome.stage == "VIDEO_READY":
                if not outcome.video_path or not outcome.video_sha:
                    raise RuntimeError("STEP07_MP4_EVIDENCE_MISSING")
                from money_ai.core import digest_file
                if digest_file(outcome.video_path) != outcome.video_sha:
                    raise RuntimeError("STEP07_MP4_SHA_MISMATCH")
            if outcome.stage == "WAITING_APPROVAL" and not outcome.review_message_id:
                raise RuntimeError("STEP07_TELEGRAM_RECEIPT_MISSING")
            print("TOOL_SYSTEM_STEP07_STAGE_VERIFIED=" + outcome.stage, flush=True)
            return {"job_id": outcome.id, "stage": outcome.stage,
                    "video_sha": outcome.video_sha,
                    "review_message_id": outcome.review_message_id}
        finally:
            store.close()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id")
    parser.add_argument("--resume-blocked", action="store_true",
                        help="Resume only a previously BLOCKED preflight failure known to be safe")
    args = parser.parse_args()
    try:
        result = execute(job_id=args.job_id, resume_blocked=args.resume_blocked)
    except Exception as error:
        raw = str(error).split(":", 1)[0].strip()
        code = raw if re.fullmatch(r"[A-Z][A-Z0-9_]{3,90}", raw) else type(error).__name__
        print("TOOL_SYSTEM_STEP07=BLOCKED", flush=True)
        print("TOOL_SYSTEM_STEP07_ERROR_CODE=" + code, flush=True)
        raise SystemExit(1) from None
    print("TOOL_SYSTEM_STEP07_JOB=" + result["job_id"], flush=True)
    print("TOOL_SYSTEM_STEP07_RESULT=" + result["stage"], flush=True)
    if result.get("video_sha"):
        print("TOOL_SYSTEM_STEP07_VIDEO_SHA256=" + result["video_sha"], flush=True)
    if result.get("review_message_id"):
        print("TOOL_SYSTEM_STEP07_TELEGRAM_RECEIPT=" + result["review_message_id"], flush=True)


if __name__ == "__main__":
    main()
