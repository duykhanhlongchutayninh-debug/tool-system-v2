"""One-worker SQLite Store with synchronous Supabase backups after every change.

The cloud checkpoint is required for progress. On failure local state remains
intact, and the caller must stop / alert, not proceed to the next stage.
"""
from pathlib import Path
from typing import Any
import json
import re
from .core import Store
from .cloud_checkpoint import CloudCheckpoint


class DurableStore(Store):
    def __init__(self, path: str | Path, checkpoint: CloudCheckpoint) -> None:
        self.checkpoint = checkpoint
        path = Path(path)
        if not path.exists():
            # Never overwrite an existing local SQLite DB, even when the cloud
            # version is newer. Recovery requires no local DB at all.
            checkpoint.restore(path)
        super().__init__(path)

    def create(self, *args: Any, **kwargs: Any):
        job = super().create(*args, **kwargs)
        self.checkpoint.backup(self.path)
        return job

    def move(self, *args: Any, **kwargs: Any):
        job = super().move(*args, **kwargs)
        self.checkpoint.backup(self.path)
        return job

    def update_revision_feedback(self, job_id: str, feedback: str):
        job = super().update_revision_feedback(job_id, feedback)
        self.checkpoint.backup(self.path)
        return job

    def record_event(self, job_id: str, kind: str, details: str) -> dict:
        """Durably log an external side-effect receipt without changing job stage."""
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", str(kind or "")):
            raise ValueError("DURABLE_EVENT_KIND_INVALID")
        if not isinstance(details, str) or not details.strip():
            raise ValueError("DURABLE_EVENT_DETAILS_REQUIRED")
        with self._lock, self._db:
            self.get(job_id)
            self._log(job_id, kind, details)
        # Cloud failure stops the caller; the local event is retained for
        # reconciliation before any follow-up external side effect.
        self.checkpoint.backup(self.path)
        return self.events(job_id)[-1]

    def remember_fix(self, *args: Any, **kwargs: Any) -> None:
        super().remember_fix(*args, **kwargs)
        self.checkpoint.backup(self.path)

    def rebind_verified_source_path(self, job_id: str, source_path: str | Path,
                                    source_sha: str) -> Any:
        """Rebind a recovered local path only when bytes match an existing source job.

        Kaggle's /working path can change after a kernel reset. This narrowly
        updates SOURCE_READY metadata for the same immutable SHA; it cannot
        change the source identity or advance the job to another stage.
        """
        from .core import digest_file

        path = Path(source_path)
        if (not path.is_file() or not re.fullmatch(r"[a-f0-9]{64}", source_sha)
                or digest_file(path) != source_sha):
            raise ValueError("SOURCE_PATH_REBIND_SHA_MISMATCH")
        job = self.get(job_id)
        if job.stage != "SOURCE_READY" or job.source_sha != source_sha:
            raise ValueError("SOURCE_PATH_REBIND_JOB_MISMATCH")
        if job.source_path == str(path):
            return job
        with self._lock, self._db:
            # Re-read under the store lock before changing the source path.
            current = self.get(job_id)
            if current.stage != "SOURCE_READY" or current.source_sha != source_sha:
                raise ValueError("SOURCE_PATH_REBIND_JOB_MISMATCH")
            self._db.execute(
                "UPDATE jobs SET source_path=?, updated_at=datetime('now') WHERE id=?",
                (str(path), job_id),
            )
            self._log(job_id, "SOURCE_PATH_REBOUND",
                      json.dumps({"source_sha": source_sha, "source_path": str(path)},
                                 sort_keys=True))
        self.checkpoint.backup(self.path)
        return self.get(job_id)

    def record_source_archive(self, job_id: str, sha: str, remote_key: str) -> None:
        """Persist an independently verified source archive receipt immediately.

        A screenshot or open Page tab is not recoverable after Kaggle ends.
        Archive receipt (job/reel/sha/object key) is copied into the durable
        SQLite event log and backed up to Supabase before any caption work.
        """
        if (not re.fullmatch(r"[a-f0-9]{64}", sha)
                or remote_key != f"videos/{job_id}/{sha}.mp4"):
            raise ValueError("SOURCE_ARCHIVE_RECEIPT_INVALID")
        job = self.get(job_id)
        # A source archive receipt may be refreshed when the SAME immutable MP4
        # already belongs to a job that advanced beyond Step05. This writes only
        # an audit event; it does not move the job backwards or change its source.
        # Refuse every mismatch, including a SOURCE_READY job with another SHA.
        later_stages = {
            "SCRIPT_READY", "VIDEO_READY", "WAITING_APPROVAL",
            "REVISION_REQUESTED", "APPROVED", "PUBLISHING", "PUBLISHED",
        }
        source_ready_recovery = (
            job.stage == "BLOCKED"
            and job.resume_stage == "SOURCE_READY"
            and job.source_sha == sha
        )
        blocked_later_recovery = (
            job.stage == "BLOCKED"
            and job.resume_stage in later_stages
            and job.source_sha == sha
        )
        advanced_same_source = (
            job.stage in later_stages
            and job.source_sha == sha
        )
        if job.stage == "SOURCE_READY" and job.source_sha != sha:
            raise ValueError("SOURCE_ARCHIVE_SOURCE_SHA_MISMATCH")
        if job.stage not in ("NEW", "SOURCE_READY") and not (
                source_ready_recovery or blocked_later_recovery or advanced_same_source):
            if job.stage in later_stages or job.stage == "BLOCKED":
                raise ValueError("SOURCE_ARCHIVE_SOURCE_SHA_MISMATCH")
            raise ValueError("SOURCE_ARCHIVE_JOB_STAGE_INVALID")
        evidence = json.dumps({
            "source_url": job.source_url,
            "sha256": sha, "remote_key": remote_key,
        }, sort_keys=True)
        with self._lock, self._db:
            self._log(job_id, "SOURCE_MP4_ARCHIVED", evidence)
        self.checkpoint.backup(self.path)
