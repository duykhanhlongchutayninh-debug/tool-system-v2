"""Supabase checkpoint backup for the single Tool System V2 Kaggle CPU worker.

The source of truth during one CPU session remains the local SQLite DB.  A
consistent, immutable SQLite snapshot is uploaded to private Supabase Storage,
read back and SHA-256 checked, then the checkpoint pointer is committed.  A
failed upload/verification cannot replace the previous known-good pointer.

This module does not access Kaggle Secrets. Call it with project-specific
TOOL_SYSTEM_SUPABASE_SERVICE_ROLE_KEY in the current process environment. Do not
log keys. It neither touches Chrome nor starts background services.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import os
import re
import sqlite3
import tempfile
import time
from urllib.parse import quote

import requests

PROJECT_URL = os.getenv("TOOL_SYSTEM_SUPABASE_URL", "").strip().rstrip("/")
BUCKET = "money-ai-state"
LIMIT = 25 * 1024 * 1024
REQUIRED_TABLES = frozenset({"jobs", "events", "lessons"})


class CheckpointError(RuntimeError):
    pass


class CloudCheckpoint:
    def __init__(self, *, service_key: str | None = None, url: str | None = None,
                 session: object | None = None) -> None:
        configured_url = (os.getenv("TOOL_SYSTEM_SUPABASE_URL") or "").strip().rstrip("/")
        self.url = (url or configured_url or PROJECT_URL).rstrip("/")
        if not self.url:
            raise CheckpointError("TOOL_SYSTEM_SUPABASE_URL_MISSING")
        # A per-call URL may only match the separately configured Tool System project.
        if configured_url and self.url != configured_url:
            raise CheckpointError("WRONG_SUPABASE_PROJECT")
        if not configured_url and url:
            raise CheckpointError("TOOL_SYSTEM_SUPABASE_URL_NOT_CONFIGURED")
        self._key = service_key or os.getenv("TOOL_SYSTEM_SUPABASE_SERVICE_ROLE_KEY")
        if not self._key:
            raise CheckpointError("TOOL_SYSTEM_SUPABASE_SERVICE_ROLE_KEY_MISSING")
        self.session = session if session is not None else requests.Session()

    def _request(self, method: str, path: str, *, payload: bytes | None = None,
                 json_body: object | None = None, extra_headers: dict | None = None):
        # Modern sb_secret_* API keys are opaque, not JWTs. Send them as
        # apikey only; old service_role JWTs may also use Authorization.
        headers = {"apikey": self._key}
        if not self._key.startswith("sb_secret_"):
            headers["Authorization"] = "Bearer " + self._key
        if extra_headers:
            headers.update(extra_headers)
        try:
            response = self.session.request(method, self.url + path,
                headers=headers, data=payload, json=json_body, timeout=45)
        except requests.RequestException as exc:
            # Never format exception URLs (some SDKs append credentials).
            raise CheckpointError("SUPABASE_NETWORK_ERROR:" + type(exc).__name__) from None
        if not 200 <= response.status_code < 300:
            # Avoid leaking remote error message or request authentication headers.
            raise CheckpointError("SUPABASE_HTTP_" + str(response.status_code))
        return response

    @staticmethod
    def _assert_sqlite(db: Path) -> None:
        if not db.is_file() or db.stat().st_size == 0:
            raise CheckpointError("SQLITE_SNAPSHOT_MISSING")
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
                row = conn.execute("pragma quick_check").fetchone()
                if row != ("ok",):
                    raise CheckpointError("SQLITE_INTEGRITY_FAILED")
                tables = {r[0] for r in conn.execute(
                    "select name from sqlite_master where type='table'")}
                if not REQUIRED_TABLES.issubset(tables):
                    raise CheckpointError("SQLITE_SCHEMA_INCOMPLETE")
        except sqlite3.DatabaseError:
            raise CheckpointError("SQLITE_FORMAT_INVALID") from None

    @staticmethod
    def _sqlite_copy(source: Path, target: Path) -> None:
        if not source.is_file():
            raise CheckpointError("SQLITE_SOURCE_MISSING")
        # sqlite backup() handles WAL transactions without copying a partial DB.
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
            with sqlite3.connect(str(target)) as dest:
                src.backup(dest)

    def backup(self, sqlite_path: str | Path) -> str:
        """Write immutable snapshot, verify readback, then advance pointer.

        Do not call concurrently with another CPU worker. A save failure must
        stop the caller from advancing to another irreversible action.
        """
        path = Path(sqlite_path)
        with tempfile.TemporaryDirectory(prefix="money-ai-state-") as directory:
            snapshot = Path(directory) / "snapshot.sqlite"
            self._sqlite_copy(path, snapshot)
            self._assert_sqlite(snapshot)
            length = snapshot.stat().st_size
            if length > LIMIT:
                raise CheckpointError("SQLITE_STATE_EXCEEDS_25_MIB")
            blob = snapshot.read_bytes()
            digest = sha256(blob).hexdigest()
            key = "snapshots/" + str(time.time_ns()) + "_" + digest[:16] + ".sqlite"
            encoded = quote(key, safe="/")
            self._request("POST", f"/storage/v1/object/{BUCKET}/{encoded}",
                          payload=blob, extra_headers={
                              "Content-Type": "application/octet-stream",
                              "x-upsert": "false"})
            # Reading back prevents a false success after a server accepted only
            # partial bytes / a proxy reported an incorrect response.
            confirmation = self._request("GET", f"/storage/v1/object/{BUCKET}/{encoded}")
            if len(confirmation.content) != length or sha256(confirmation.content).hexdigest() != digest:
                raise CheckpointError("REMOTE_CHECKPOINT_HASH_MISMATCH")
            # PostgREST upsert does not update the column's DEFAULT on
            # conflict. Supply updated_at explicitly, otherwise the pointer can
            # reference today's snapshot while its timestamp misleadingly stays
            # at the day the row was first created.
            self._request("POST", "/rest/v1/money_ai_checkpoints?on_conflict=name",
                json_body={"name": "primary", "object_path": key,
                           "sha256": digest, "byte_size": length,
                           "updated_at": datetime.now(timezone.utc).isoformat()},
                extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal",
                               "Content-Type": "application/json"})
            return digest

    def latest(self) -> dict | None:
        response = self._request("GET", "/rest/v1/money_ai_checkpoints?"
                                 "name=eq.primary&select=object_path,sha256,byte_size")
        try:
            rows = response.json()
        except (ValueError, TypeError):
            raise CheckpointError("CHECKPOINT_INDEX_INVALID") from None
        if not isinstance(rows, list) or len(rows) > 1:
            raise CheckpointError("CHECKPOINT_INDEX_INVALID")
        return rows[0] if rows else None

    def restore(self, sqlite_path: str | Path) -> bool:
        """Restore only to an ABSENT DB; never overwrite live local state.

        Returns False if no remote checkpoint exists. Call before opening Store.
        Existing jobs referencing missing local MP4s must be reconciled by Agent;
        this only restores its durable state, not large media files.
        """
        dest = Path(sqlite_path)
        if dest.exists():
            raise CheckpointError("REFUSE_OVERWRITE_EXISTING_LOCAL_STATE")
        record = self.latest()
        if record is None:
            return False
        key, checksum, expected_size = (record.get("object_path"),
                                      record.get("sha256"), record.get("byte_size"))
        if (not isinstance(key, str) or not re.fullmatch(
                r"snapshots/[0-9]{10,25}_[a-f0-9]{16}\.sqlite", key)
            or not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum)
            or not isinstance(expected_size, int) or not (0 < expected_size <= LIMIT)):
            raise CheckpointError("CHECKPOINT_POINTER_INVALID")
        response = self._request("GET", f"/storage/v1/object/{BUCKET}/" + quote(key, safe="/"))
        blob = response.content
        if len(blob) != expected_size or sha256(blob).hexdigest() != checksum:
            raise CheckpointError("REMOTE_CHECKPOINT_HASH_MISMATCH")
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".money-ai-restore-", suffix=".sqlite",
                                             dir=dest.parent, delete=False) as handle:
                temp_name = Path(handle.name)
                handle.write(blob)
            self._assert_sqlite(temp_name)
            if dest.exists():
                raise CheckpointError("REFUSE_OVERWRITE_EXISTING_LOCAL_STATE")
            os.replace(temp_name, dest)
            temp_name = None
            return True
        finally:
            if temp_name is not None:
                temp_name.unlink(missing_ok=True)
