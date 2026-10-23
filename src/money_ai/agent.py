"""Single-worker orchestration. Every step produces evidence or a visible error."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Protocol
import uuid

from .core import GateError, Job, Store, digest_file
from .video import minimum_video_duration, probe_mp4


@dataclass(frozen=True)
class SourceResult:
    path: Path
    hashtags: str

@dataclass(frozen=True)
class ScriptResult:
    original_analysis: str
    screenplay: str

@dataclass(frozen=True)
class VideoResult:
    path: Path
    provider: str
    duration: float
    width: int
    height: int


class Tools(Protocol):
    def collect(self, job: Job) -> SourceResult: ...
    def screenplay(self, job: Job) -> ScriptResult: ...
    def generate(self, job: Job) -> VideoResult: ...
    def send_for_review(self, job: Job) -> str: ...
    def publish(self, job: Job) -> str: ...
    def error(self, job: Job, message: str) -> None: ...


class Agent:
    """Autonomous per-Page stage selection; no separate orchestrator or worker."""
    def __init__(self, store: Store, tools: Tools) -> None:
        self.store = store
        self.tools = tools
        self._in_process_publish_attempts: set[tuple[str, str]] = set()

    @classmethod
    def production(cls, store: Store, tools: Tools) -> "Agent":
        """Create the deterministic Python-only coordinator.

        External tools remain available through Tools. The coordinator runs
        only local Python rules and never loads a remote reasoning component.
        """
        print("MONEY_TOOL_COORDINATOR=PYTHON_RULES_ONLY", flush=True)
        return cls(store, tools)

    def step(self, job_id: str) -> Job:
        job = self.store.get(job_id)
        if job.stage in ("PUBLISHED", "WAITING_APPROVAL", "BLOCKED"):
            return job
        if job.stage == "PUBLISHING":
            # A process restart between submit and confirmation can mean success.
            # Never submit the same Reel a second time without reconciliation.
            return self._error(job, "Uncertain Facebook submission: reconcile post before retry")
        if job.stage == "APPROVED" and not self._due(job.schedule_at):
            return job
        try:
            # Stage transitions are selected only by the deterministic Python
            # state machine below. The production coordinator never invokes a model.
            if job.stage == "NEW":
                src = self.tools.collect(job)
                return self.store.move(job.id, "SOURCE_READY", note="Source MP4 hash verified",
                                       source_path=str(src.path), source_sha=digest_file(src.path),
                                       hashtags=src.hashtags)
            if job.stage in ("SOURCE_READY", "REVISION_REQUESTED"):
                result = self.tools.screenplay(job)
                return self.store.move(job.id, "SCRIPT_READY", note="Gemini response recorded",
                                       original_analysis=result.original_analysis,
                                       script=result.screenplay,
                                       script_sha=sha256(result.screenplay.encode()).hexdigest(),
                                       video_path=None, video_sha=None, duration=None,
                                       frame_w=None, frame_h=None, owner_approved_sha=None,
                                       review_message_id=None, publish_attempt=None)
            if job.stage == "SCRIPT_READY":
                result = self.tools.generate(job)
                minimum = minimum_video_duration(result.provider)
                print(
                    "TOOL_SYSTEM_AGENT_VIDEO_QA_MIN_SECONDS=" + f"{minimum:.1f}",
                    flush=True,
                )
                proof = probe_mp4(result.path, min_duration=minimum)
                return self.store.move(job.id, "VIDEO_READY", note="MP4 checked via ffprobe",
                                       video_path=str(result.path), video_sha=digest_file(result.path),
                                       duration=proof.duration, frame_w=proof.width,
                                       frame_h=proof.height, provider=result.provider)
            if job.stage == "VIDEO_READY":
                receipt = self.tools.send_for_review(job)
                return self.store.move(job.id, "WAITING_APPROVAL", note="Telegram receipt verified",
                                       review_message_id=str(receipt))
            if job.stage == "APPROVED":
                if digest_file(job.video_path or "") != job.video_sha:
                    raise GateError("Downloaded video changed after approval")
                # Stable unique attempt ID is saved BEFORE external Facebook submit.
                attempt = uuid.uuid4().hex
                reserved = self.store.move(job.id, "PUBLISHING", note="Facebook attempt reserved",
                                           publish_attempt=attempt)
                self._in_process_publish_attempts.add((job.id, attempt))
                return reserved
            raise GateError(f"Unsupported stage {job.stage}")
        except Exception as exc:
            return self._error(job, str(exc))

    def execute_publish(self, job_id: str) -> Job:
        """Invoked only on a persisted PUBLISHING job in the SAME process.

        Never call after restart without checking Facebook for an existing post.
        """
        job = self.store.get(job_id)
        if job.stage != "PUBLISHING" or not job.publish_attempt:
            raise GateError("Must first reserve a unique publish attempt")
        token=(job.id,job.publish_attempt)
        if token not in self._in_process_publish_attempts:
            raise GateError("Publish attempt belongs to prior process; reconcile with Facebook")
        self._in_process_publish_attempts.remove(token)  # never submit twice
        if digest_file(job.video_path or "") != job.video_sha or job.video_sha != job.owner_approved_sha:
            return self._error(job, "MP4 changed during posting")
        try:
            permalink = self.tools.publish(job)
            return self.store.move(job.id, "PUBLISHED", note="Facebook permalink captured",
                                   post_url=permalink)
        except Exception as exc:
            return self._error(job, f"Ambiguous Facebook publication; inspect Page: {exc}")

    def _error(self, job: Job, error: str) -> Job:
        try:
            blocked = self.store.block(job.id, error)
        except Exception as persist_exc:
            # DurableStore may have saved locally but failed to back up to
            # Supabase.  Do not skip notifying the owner just because a second
            # attempt to checkpoint BLOCKED has also failed.
            reason = (f"{error}; STATE_CHECKPOINT_FAILED:"
                      f"{type(persist_exc).__name__}")
            try:
                self.tools.error(self.store.get(job.id), reason)
            except Exception as alert_exc:
                raise RuntimeError("STATE_CHECKPOINT_FAILED_AND_TELEGRAM_ALERT_FAILED") from alert_exc
            raise RuntimeError("STATE_CHECKPOINT_FAILED; owner notified; "
                               "stop workflow until cloud recovery") from persist_exc
        try:
            self.tools.error(blocked, error)
        except Exception as alert_exc:
            # Never swallow a failure to alert owner.
            raise RuntimeError(f"Job blocked: {error}; TELEGRAM ALERT FAILED: {alert_exc}") from alert_exc
        if blocked.stage == "PUBLISHED":
            # The Facebook submission may have completed, but the Supabase
            # checkpoint after setting PUBLISHED failed. Never return an
            # ordinary successful state or re-submit the post automatically.
            raise RuntimeError("PUBLISHED_LOCALLY_BUT_CLOUD_CHECKPOINT_UNVERIFIED;"
                               " owner notified; reconcile before continuing")
        return blocked

    @staticmethod
    def _decision_fingerprint(job: Job) -> tuple:
        """Fields that must remain unchanged between state inspection and action."""
        names = (
            "id", "page", "stage", "source_url", "source_path", "source_sha",
            "hashtags", "original_analysis", "script", "script_sha", "video_path",
            "video_sha", "owner_approved_sha", "schedule_at", "review_message_id",
            "publish_attempt", "resume_stage",
        )
        return tuple(getattr(job, name, None) for name in names)

    @staticmethod
    def _due(value: str | None) -> bool:
        if not value:
            return False
        d = datetime.fromisoformat(value)
        return d.tzinfo is not None and d.astimezone(timezone.utc) <= datetime.now(timezone.utc)

    def run_ready(self) -> list[Job]:
        """One bounded pass: waiting approval on Page A doesn't stop Page B."""
        results = []
        for job in self.store.list_jobs():
            if job.stage not in ("PUBLISHED", "WAITING_APPROVAL", "BLOCKED"):
                results.append(self.step(job.id))
        return results
