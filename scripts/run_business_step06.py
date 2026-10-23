"""Business Step06 — Gemini sees/hears archived source MP4 and writes a new script.

Kaggle Notebook invokes this single process after Step05 SOURCE_READY VERIFIED.
No re-crawl, source caption, extra browser, Meta/Flow or Facebook publish.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from hashlib import sha256
import fcntl

ROOT = Path(__file__).resolve().parents[1]
for part in (ROOT, ROOT / "src"):
    if str(part) not in sys.path:
        sys.path.insert(0, str(part))

from money_ai.agent import Agent
from money_ai.core import digest_file, GateError
from money_ai.cloud_checkpoint import CloudCheckpoint
from money_ai.cloud_media import CloudMedia
from money_ai.durable_store import DurableStore
from money_ai.gemini_web import screenplay_from_mp4
from money_ai.tool_recovery import recover_editor  # release capability gate
from money_ai.infra_telegram import notify

DB = Path("/kaggle/working/tool-system-v2-runtime/state/jobs.sqlite")
LOCK = Path("/kaggle/working/tool-system-v2-runtime/state/business-step06.lock")
PAGE = "Mẹo nhỏ mỗi ngày"
_ERROR = re.compile(r"[A-Z][A-Z0-9_]{3,90}\Z")


class GeminiOnlyTools:
    def __init__(self, store):
        self.store=store

    def screenplay(self, job):
        return screenplay_from_mp4(job, lessons=self.store.lessons(),
                                   remember=self.store.remember_fix)

    def error(self, job, message):
        raw = str(message).split(":", 1)[0].strip()
        code = raw if _ERROR.fullmatch(raw) else "GEMINI_BROWSER_OR_PROVIDER_ERROR"
        print("TOOL_SYSTEM_GEMINI_AGENT_ERROR=" + code, flush=True)
        delivered = notify("NGHIỆP VỤ GEMINI",
                           "❌ Page " + job.page + ": " + code)
        print("TOOL_SYSTEM_GEMINI_TELEGRAM=" + delivered, flush=True)
        if delivered != "SENT":
            raise RuntimeError("GEMINI_TELEGRAM_ERROR_NOTIFICATION_FAILED")

    def collect(self, job):
        raise GateError("GEMINI_CANNOT_RECRAWL")

    def generate(self, job):
        raise GateError("GEMINI_CANNOT_GENERATE_VIDEO")

    def send_for_review(self, job):
        raise GateError("GEMINI_CANNOT_APPROVE")

    def publish(self, job):
        raise GateError("GEMINI_CANNOT_PUBLISH")


def execute(*, job_id: str | None = None) -> dict:
    release_name = ROOT.name
    release_sha = (release_name[:12] if re.fullmatch(r"[0-9a-f]{40}", release_name)
                   else "SOURCE_PATH_UNPINNED")
    print("TOOL_SYSTEM_STEP06_ACTIVE_RELEASE_SHA=" + release_sha, flush=True)
    # This marker cannot print on an older release without the new neural
    # recovery module. It prevents misreading stale 13:09 UI logs as new tests.
    print("TOOL_SYSTEM_BUSINESS_STEP06_AGENT_LOOP=RESEARCH_OBSERVE_REPLAN",
          flush=True)
    if not os.environ.get("TOOL_SYSTEM_SUPABASE_SERVICE_ROLE_KEY"):
        raise RuntimeError("GEMINI_SUPABASE_CHECKPOINT_KEY_MISSING")
    if job_id is not None and not re.fullmatch(r"[a-f0-9]{16}", job_id):
        raise RuntimeError("GEMINI_INVALID_JOB_ID")
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("GEMINI_STEP06_ALREADY_RUNNING") from None
        os.environ["DISPLAY"] = ":99"
        os.environ["TOOL_SYSTEM_CDP_URL"] = "http://127.0.0.1:9222"
        store = DurableStore(DB, CloudCheckpoint())
        try:
            if job_id is None:
                # Prefer the exact SHA-bound job recorded by Step05. This binds
                # the buttons into one workflow and avoids guessing when the
                # local/cloud job database contains older Page jobs.
                active_job_path = Path(DB).parent / "active_business_job.json"
                if active_job_path.is_file():
                    try:
                        active = json.loads(active_job_path.read_text(encoding="utf-8"))
                        active_id = active.get("job_id")
                        active_sha = active.get("source_sha256")
                    except (OSError, ValueError, TypeError):
                        raise RuntimeError("GEMINI_ACTIVE_JOB_CHECKPOINT_INVALID") from None
                    if (active.get("evidence") != "STEP05_MP4_SHA_AND_SUPABASE_RECEIPT_VERIFIED"
                            or active.get("valid") is not True
                            or not isinstance(active_id, str)
                            or not re.fullmatch(r"[a-f0-9]{16}", active_id)
                            or not isinstance(active_sha, str)
                            or not re.fullmatch(r"[a-f0-9]{64}", active_sha)):
                        raise RuntimeError("GEMINI_ACTIVE_JOB_CHECKPOINT_INVALID")
                    active_job = store.get(active_id)
                    if (active_job.page != PAGE or active_job.source_sha != active_sha
                            or (active.get("source_url")
                                and active_job.source_url != active["source_url"])):
                        raise RuntimeError("GEMINI_ACTIVE_JOB_SOURCE_MISMATCH")
                    job_id = active_job.id
                    print("TOOL_SYSTEM_GEMINI_ACTIVE_JOB=SHA_BOUND_STEP05_CHECKPOINT", flush=True)
                else:
                    # Legacy notebooks without a Step05 receipt can use only one
                    # unambiguous source job; never select the first item by luck.
                    ready = [item for item in store.list_jobs()
                             if item.page == PAGE and (
                                 item.stage in ("SOURCE_READY", "REVISION_REQUESTED") or
                                 (item.stage == "BLOCKED" and
                                  item.resume_stage in ("SOURCE_READY", "REVISION_REQUESTED")))]
                    if len(ready) != 1:
                        raise RuntimeError("GEMINI_SOURCE_JOB_SELECTION_REQUIRED")
                    job_id = ready[0].id
            job = store.get(job_id)
            print("TOOL_SYSTEM_BUSINESS_STEP06_SELECTED_JOB=" + job.id, flush=True)
            if job.page != PAGE:
                raise RuntimeError("GEMINI_UNEXPECTED_PAGE")
            if (job.stage == "REVISION_REQUESTED"
                    and (not str(job.feedback or "").strip()
                         or str(job.feedback or "").strip() == "Owner requested revision")):
                # Keep the durable state REVISION_REQUESTED so the owner can
                # still reply to the exact Telegram video and supply feedback.
                raise RuntimeError("GEMINI_REVISION_FEEDBACK_REQUIRED")
            reusable_script_stages = {
                "SCRIPT_READY", "VIDEO_READY", "WAITING_APPROVAL",
                "APPROVED", "PUBLISHING", "PUBLISHED",
            }
            blocked_after_script = (
                job.stage == "BLOCKED" and job.resume_stage in reusable_script_stages
            )
            if job.stage in reusable_script_stages or blocked_after_script:
                if not job.original_analysis or not job.script or not job.script_sha:
                    raise RuntimeError("GEMINI_PREVIOUS_SCRIPT_EVIDENCE_MISSING")
                if sha256(job.script.encode()).hexdigest() != job.script_sha:
                    raise RuntimeError("GEMINI_PREVIOUS_SCRIPT_SHA_MISMATCH")
                # The script bytes are valid, but a BLOCKED job is NOT
                # SCRIPT_READY in the state machine. Never hide the previous
                # Step07 block behind a false success marker.
                if blocked_after_script:
                    raw_error = str(job.error or "")
                    candidates = re.findall(
                        r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9_]{3,90})(?![A-Za-z0-9_])",
                        raw_error,
                    )
                    known_errors = {
                        "VIDEO_MP4_DOWNLOAD_NOT_OBSERVED",
                        "VIDEO_DOWNLOAD_IS_NOT_MP4",
                        "VIDEO_MP4_DURATION_OR_ASPECT_RATIO_INVALID",
                        "VIDEO_PROVIDER_NAVIGATED_OUTSIDE_APPROVED_DOMAIN",
                        "VIDEO_SCRIPT_CHANGED_DURING_GENERATION",
                        "STATE_CHECKPOINT_FAILED",
                        "VIDEO_PROVIDER_MUST_BE_FLOW_OR_META_AI",
                        "VIDEO_SCRIPT_SHA_MISMATCH",
                        "VIDEO_GENERATION_REQUIRES_SCRIPT_READY",
                    }
                    prior_code = next((code for code in candidates if code in known_errors),
                                      "UNCLASSIFIED_PRIOR_FAILURE")
                    print("TOOL_SYSTEM_GEMINI=SCRIPT_CHECKPOINT_REUSED_FROM_BLOCKED_JOB", flush=True)
                    print("TOOL_SYSTEM_BUSINESS_STEP06_BLOCKED_RESUME_STAGE=" +
                          str(job.resume_stage or "UNKNOWN"), flush=True)
                    print("TOOL_SYSTEM_BUSINESS_STEP06_PRIOR_FAILURE_CODE=" +
                          prior_code, flush=True)
                    print("TOOL_SYSTEM_BUSINESS_STEP06=SCRIPT_CHECKPOINT_VERIFIED_JOB_REMAINS_BLOCKED",
                          flush=True)
                    return {"job_id": job.id, "stage": job.stage,
                            "sha256": job.script_sha, "script_checkpoint": "REUSED_VERIFIED",
                            "blocked": True, "prior_failure_code": prior_code}
                print("TOOL_SYSTEM_GEMINI=SCRIPT_READY_REUSED_FROM_CHECKPOINT", flush=True)
                print("TOOL_SYSTEM_BUSINESS_STEP06=SCRIPT_READY_VERIFIED", flush=True)
                return {"job_id": job.id, "stage": job.stage,
                        "sha256": job.script_sha, "script_checkpoint": "REUSED_VERIFIED"}
            # After a Kaggle CPU→GPU reset, SQLite is restored from cloud,
            # but its job can point to an MP4 in the old ephemeral filesystem.
            # CRITICAL: Store.resume(BLOCKED→SOURCE_READY) verifies the source
            # file BEFORE permitting the transition. Restore the exact archived
            # MP4 and its SHA FIRST, or the owner receives a generic GateError.
            if (job.stage == "BLOCKED"
                    and job.resume_stage not in ("SOURCE_READY", "REVISION_REQUESTED")):
                raise RuntimeError("GEMINI_UNSAFE_BLOCKED_STAGE")
            if (job.stage not in ("SOURCE_READY", "REVISION_REQUESTED", "BLOCKED")
                    or not job.source_path or not job.source_sha):
                raise RuntimeError("GEMINI_SOURCE_READY_REQUIRED")
            source = Path(job.source_path)
            print("TOOL_SYSTEM_STEP06_PHASE=RESTORE_OR_VERIFY_ARCHIVED_MP4",flush=True)
            if not source.is_file():
                # Historical SOURCE_READY jobs may have been saved under a
                # human-readable MP4 basename. Identity is the ARCHIVE KEY
                # and SHA-256, not filename text. The old basename equality
                # blocked valid recovery and the CI test at 13:03.
                # Before writing, require an absolute .mp4 path contained
                # within the existing Kaggle working tree (or the injected
                # test SQLite parent). Resolve symlinks to prevent escapes.
                allowed_roots = [
                    Path("/kaggle/working/tool-system-v2-runtime").resolve(),
                    Path(DB).resolve().parent,
                ]
                if (not source.is_absolute() or
                    source.suffix.lower() != ".mp4" or
                    not any(source.resolve().is_relative_to(root)
                            for root in allowed_roots)):
                    raise RuntimeError("GEMINI_RESTORE_SOURCE_PATH_UNSAFE")
                CloudMedia().restore_mp4(
                    f"videos/{job.id}/{job.source_sha}.mp4", job.source_sha,
                    source)
                print("TOOL_SYSTEM_GEMINI=SOURCE_RESTORED_FROM_SUPABASE", flush=True)
            if digest_file(source) != job.source_sha:
                raise RuntimeError("GEMINI_SOURCE_SHA_MISMATCH")
            print("TOOL_SYSTEM_GEMINI=SOURCE_SHA_VERIFIED", flush=True)
            if job.stage == "BLOCKED":
                print("TOOL_SYSTEM_STEP06_PHASE=RESUME_BLOCKED_VERIFIED_SOURCE_JOB",flush=True)
                job = store.resume(job.id)
                print("TOOL_SYSTEM_GEMINI=RESUMED_PREVIOUS_" + job.stage, flush=True)
            if job.stage == "REVISION_REQUESTED":
                print("TOOL_SYSTEM_STEP06_PHASE=REGENERATE_OWNER_REQUESTED_REVISION", flush=True)
            print("TOOL_SYSTEM_STEP06_PHASE=GEMINI_AI_BROWSER_OPERATION",flush=True)
            print("TOOL_SYSTEM_BUSINESS_STEP06=STARTED", flush=True)
            # This is a known, single-stage business workflow. Python owns
            # the verified SOURCE_READY -> SCRIPT_READY state transition;
            # Gemini remains the real video analyzer/screenplay writer, while
            # The coordinator is Python-only by design; there is no optional
            # model planner endpoint or local inference fallback.
            print("TOOL_SYSTEM_BUSINESS_STEP06_COORDINATOR=PYTHON_VERIFIED_STAGE_MACHINE",
                  flush=True)
            outcome = Agent(store, GeminiOnlyTools(store)).step(job.id)
            if outcome.stage != "SCRIPT_READY":
                raise RuntimeError("GEMINI_SCRIPT_NOT_READY")
            if (not outcome.original_analysis or not outcome.script or
                    sha256(outcome.script.encode()).hexdigest() != outcome.script_sha):
                raise RuntimeError("GEMINI_SCRIPT_CHECKPOINT_MISMATCH")
            print("TOOL_SYSTEM_BUSINESS_STEP06=SCRIPT_READY_VERIFIED", flush=True)
            return {"job_id": outcome.id, "stage": outcome.stage,
                    "sha256": outcome.script_sha}
        finally:
            store.close()


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=False)
    args = parser.parse_args()
    try:
        result = execute(job_id=args.job_id)
    except Exception as error:
        raw = str(error).split(":", 1)[0].strip()
        if _ERROR.fullmatch(raw):
            code = raw
        elif isinstance(error, GateError):
            # Detailed *safe* classification instead of the useless
            # previous "GateError". No usernames, tokens or URLs in logs.
            known = {
                "Source file/hash must be real": "GEMINI_SOURCE_MISSING_AT_STAGE_RESUME",
                "Gemini analysis and integrity-checked screenplay required":
                    "GEMINI_SCRIPT_VALIDATION_GATE_FAILED",
                "No blocked work to resume": "GEMINI_JOB_RESUME_INVALID",
            }
            code = known.get(raw, "GEMINI_UNCLASSIFIED_EVIDENCE_GATE")
        else:
            code = type(error).__name__
        print("TOOL_SYSTEM_BUSINESS_STEP06=BLOCKED", flush=True)
        print("TOOL_SYSTEM_BUSINESS_STEP06_ERROR_CODE=" + code, flush=True)
        # Preserve failure stage: agent browsing vs job resume/cloud media.
        raise SystemExit(1) from None
    print("TOOL_SYSTEM_BUSINESS_STEP06_JOB=" + result["job_id"], flush=True)
    print("TOOL_SYSTEM_BUSINESS_STEP06_SCRIPT_SHA=" + result["sha256"], flush=True)


if __name__ == "__main__":
    main()
