"""Tool System V2: one auditable state machine, fail-closed publishing gates.

Only locally verifiable evidence advances a job.  Web automation and LLMs
may *propose* actions but cannot bypass approval or edit state directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import sqlite3
import threading
import uuid
from typing import Any

from .video import minimum_video_duration

UTC = timezone.utc
STAGES = (
    "NEW", "SOURCE_READY", "SCRIPT_READY", "VIDEO_READY", "WAITING_APPROVAL",
    "REVISION_REQUESTED", "APPROVED", "PUBLISHING", "PUBLISHED", "BLOCKED",
)
NEXT = {
    "NEW": {"SOURCE_READY", "BLOCKED"},
    "SOURCE_READY": {"SCRIPT_READY", "BLOCKED"},
    "SCRIPT_READY": {"VIDEO_READY", "BLOCKED"},
    "VIDEO_READY": {"WAITING_APPROVAL", "BLOCKED"},
    "WAITING_APPROVAL": {"APPROVED", "REVISION_REQUESTED", "BLOCKED"},
    "REVISION_REQUESTED": {"SCRIPT_READY", "BLOCKED"},
    "APPROVED": {"PUBLISHING", "BLOCKED"},
    "PUBLISHING": {"PUBLISHED", "BLOCKED"},
    "PUBLISHED": set(),
    "BLOCKED": {"NEW", "SOURCE_READY", "SCRIPT_READY", "VIDEO_READY", "WAITING_APPROVAL", "REVISION_REQUESTED", "APPROVED", "PUBLISHING"},
}

class GateError(RuntimeError):
    """A required evidence, approval or transition is missing."""


def digest_file(path: str | Path) -> str:
    h = sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Job:
    id: str
    page: str
    source_url: str
    stage: str
    source_path: str | None
    source_sha: str | None
    original_analysis: str | None
    script: str | None
    script_sha: str | None
    video_path: str | None
    video_sha: str | None
    duration: float | None
    frame_w: int | None
    frame_h: int | None
    owner_approved_sha: str | None
    schedule_at: str | None
    post_url: str | None
    error: str | None
    feedback: str | None
    provider: str | None
    hashtags: str | None
    review_message_id: str | None
    resume_stage: str | None
    publish_attempt: str | None
    updated_at: str


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
           id TEXT PRIMARY KEY, page TEXT NOT NULL, source_url TEXT NOT NULL,
           stage TEXT NOT NULL, source_path TEXT, source_sha TEXT,
           original_analysis TEXT, script TEXT, script_sha TEXT,
           video_path TEXT, video_sha TEXT, duration REAL,
           frame_w INTEGER, frame_h INTEGER, owner_approved_sha TEXT,
           schedule_at TEXT, post_url TEXT, error TEXT, feedback TEXT,
           provider TEXT, hashtags TEXT, review_message_id TEXT,
           resume_stage TEXT, publish_attempt TEXT,
           updated_at TEXT NOT NULL, UNIQUE(page, source_url)
        );
        CREATE TABLE IF NOT EXISTS events (
           id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
           kind TEXT NOT NULL, details TEXT NOT NULL, at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lessons (
           id INTEGER PRIMARY KEY AUTOINCREMENT, error_fingerprint TEXT UNIQUE NOT NULL,
           cause TEXT NOT NULL, tested_fix TEXT NOT NULL,
           test_evidence TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """)

    def close(self) -> None:
        self._db.close()

    def _log(self, job_id: str, kind: str, details: str) -> None:
        self._db.execute("INSERT INTO events(job_id,kind,details,at) VALUES(?,?,?,?)",
                         (job_id, kind, details[:3000], utc_now()))

    def create(self, page: str, source_url: str, schedule_at: str | None = None,
               hashtags: str | None = None) -> Job:
        if not page.strip() or not source_url.startswith("https://"):
            raise ValueError("Valid Page and HTTPS source required")
        with self._lock, self._db:
            current = self._db.execute("SELECT * FROM jobs WHERE page=? AND source_url=?",
                                       (page, source_url)).fetchone()
            if current:
                return Job(**dict(current))  # idempotent; no duplicate download or post
            jid = uuid.uuid4().hex[:16]
            self._db.execute("""INSERT INTO jobs
                (id,page,source_url,stage,schedule_at,hashtags,updated_at)
                VALUES(?,?,?,'NEW',?,?,?)""",
                (jid, page, source_url, schedule_at, hashtags, utc_now()))
            self._log(jid, "CREATED", "New Page-specific source job")
        return self.get(jid)

    def get(self, job_id: str) -> Job:
        row = self._db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(job_id)
        return Job(**dict(row))

    def list_jobs(self) -> list[Job]:
        return [Job(**dict(r)) for r in self._db.execute("SELECT * FROM jobs ORDER BY updated_at")]

    def events(self, job_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._db.execute("SELECT * FROM events WHERE job_id=? ORDER BY id", (job_id,))]

    def move(self, job_id: str, target: str, *, note: str, **fields: Any) -> Job:
        if target not in STAGES:
            raise GateError(f"Unknown stage {target}")
        allowed = set(Job.__dataclass_fields__) - {"id", "page", "source_url", "stage", "updated_at"}
        if not set(fields).issubset(allowed):
            raise GateError(f"Fields not allowed: {set(fields)-allowed}")
        with self._lock, self._db:
            before = self.get(job_id)
            if target not in NEXT[before.stage]:
                raise GateError(f"Illegal transition {before.stage} -> {target}")
            merged = {**before.__dict__, **fields, "stage": target}
            self._check_gate(before, target, merged)
            assignments = ["stage=?", "updated_at=?"] + [f"{key}=?" for key in fields]
            values = [target, utc_now()] + list(fields.values()) + [job_id]
            self._db.execute(f"UPDATE jobs SET {','.join(assignments)} WHERE id=?", values)
            self._log(job_id, target, note)
            return self.get(job_id)

    @staticmethod
    def _check_gate(before: Job, target: str, m: dict[str, Any]) -> None:
        if target == "SOURCE_READY":
            path = m.get("source_path")
            if not path or not Path(path).is_file() or digest_file(path) != m.get("source_sha"):
                raise GateError("Source file/hash must be real")
        elif target == "SCRIPT_READY":
            text = m.get("script") or ""
            analysis = m.get("original_analysis") or ""
            if not analysis.strip() or len(text.strip()) < 100 or sha256(text.encode()).hexdigest() != m.get("script_sha"):
                raise GateError("Gemini analysis and integrity-checked screenplay required")
            if before.stage == "REVISION_REQUESTED" and m.get("video_sha") is not None:
                raise GateError("Old video hash must be invalidated for revision")
        elif target == "VIDEO_READY":
            path = m.get("video_path")
            if not path or not Path(path).is_file() or digest_file(path) != m.get("video_sha"):
                raise GateError("VIDEO_READY_FILE_OR_SHA_INVALID")
            minimum = minimum_video_duration(m.get("provider"))
            if not m.get("duration") or m["duration"] < minimum:
                raise GateError(
                    f"VIDEO_DURATION_BELOW_MINIMUM: actual={float(m.get('duration') or 0):.3f}s; "
                    f"required={minimum:.3f}s"
                )
            if (not m.get("frame_w") or not m.get("frame_h")
                    or not (0.5 < m["frame_w"] / m["frame_h"] < 0.65)):
                raise GateError("VIDEO_PORTRAIT_9_16_REQUIRED")
        elif target == "WAITING_APPROVAL":
            if not m.get("review_message_id"):
                raise GateError("Telegram review receipt required")
        elif target == "APPROVED":
            if m.get("owner_approved_sha") != m.get("video_sha") or not m.get("video_sha"):
                raise GateError("Only owner can approve exact current video hash")
        elif target == "PUBLISHING":
            if m.get("owner_approved_sha") != m.get("video_sha"):
                raise GateError("Video approval changed")
            if not m.get("schedule_at"):
                raise GateError("Owner-set publication schedule required")
            when = datetime.fromisoformat(m["schedule_at"])
            if when.tzinfo is None or when.astimezone(UTC) > datetime.now(UTC):
                raise GateError("Scheduled time not yet reached")
            if not m.get("publish_attempt"):
                raise GateError("Durable idempotency attempt required")
        elif target == "PUBLISHED":
            if not m.get("post_url") or not m["post_url"].startswith("https://www.facebook.com/"):
                raise GateError("Verifiable Facebook post URL required")
        elif target == "BLOCKED":
            if not m.get("error"):
                raise GateError("Reason for failure required")
            if m.get("resume_stage") != before.stage:
                raise GateError("Must record exact point of failure")
        elif before.stage == "BLOCKED" and target != before.resume_stage:
            raise GateError("Blocked task may resume only at original stage")

    def block(self, job_id: str, reason: str) -> Job:
        before = self.get(job_id)
        if before.stage in ("BLOCKED", "PUBLISHED"):
            return before
        return self.move(job_id, "BLOCKED", note="ERROR: " + reason[:250],
                         error=reason[:1000], resume_stage=before.stage)

    def resume(self, job_id: str) -> Job:
        j = self.get(job_id)
        if j.stage != "BLOCKED" or not j.resume_stage:
            raise GateError("No blocked work to resume")
        # PUBLISHING is deliberately NOT auto-resumed: operator must reconcile
        # whether Facebook accepted the first upload, otherwise duplicates result.
        if j.resume_stage == "PUBLISHING":
            raise GateError("Must reconcile Facebook duplicate post before retrying publishing")
        return self.move(job_id, j.resume_stage, note="Resume with verified fix", error=None)

    def update_revision_feedback(self, job_id: str, feedback: str) -> Job:
        """Record the owner's reply to this job's review video while revision is pending."""
        clean = str(feedback or "").strip()
        if not clean or len(clean) > 2000:
            raise GateError("REVISION_FEEDBACK_EMPTY_OR_TOO_LONG")
        with self._lock, self._db:
            before = self.get(job_id)
            if before.stage != "REVISION_REQUESTED":
                raise GateError("REVISION_FEEDBACK_REQUIRES_REVISION_REQUESTED")
            if before.feedback == clean:
                return before
            self._db.execute(
                "UPDATE jobs SET feedback=?, updated_at=? WHERE id=?",
                (clean, utc_now(), job_id),
            )
            self._log(job_id, "REVISION_FEEDBACK_RECEIVED",
                      "Owner feedback saved: " + clean)
            return self.get(job_id)

    def remember_fix(self, fingerprint: str, cause: str, fix: str, proof: str) -> None:
        if not all([fingerprint, cause, fix, proof]):
            raise ValueError("Only evidence-backed fixes enter long-term memory")
        with self._lock, self._db:
            self._db.execute("""INSERT OR REPLACE INTO lessons
            (error_fingerprint,cause,tested_fix,test_evidence,created_at)
            VALUES(?,?,?,?,?)""", (fingerprint, cause, fix, proof, utc_now()))

    def lessons(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._db.execute("SELECT * FROM lessons ORDER BY id DESC LIMIT 20")]
