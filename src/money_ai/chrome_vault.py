"""Encrypted, crash-safe single-Chrome profile checkpoints in EXISTING Supabase.

This stores the OWNER'S authentication material. Never publish objects, send
cookies to logs/LLMs, or commit these bytes to GitHub. The Supabase server key
stays only in the Kaggle process; it also derives the profile encryption key.
Backups are live best-effort file/SQLite snapshots, not proof of login status.
"""
from __future__ import annotations

from hashlib import sha256
import json
import os
from contextlib import nullcontext
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
import zipfile

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

from .cloud_checkpoint import CloudCheckpoint, CheckpointError, BUCKET

TABLE = "/rest/v1/money_ai_chrome_profiles"
CHUNK_SIZE = 8 * 1024 * 1024
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
MAX_FILES = 15000
HEADER = b"TOOL_SYSTEM_CHROME_V1\0"
AAD = b"tool-system-v2/chrome-profile/v1"
SKIP_DIRS = {"Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache",
             "DawnCache", "Media Cache", "Crashpad", "CacheStorage", "ScriptCache",
             "OptimizationGuidePredictionModels", "component_crx_cache", "BrowserMetrics"}
SKIP_NAMES = {"DevToolsActivePort", "LOCK", "LOCKFILE", "SingletonLock", "SingletonCookie",
              "SingletonSocket", "chrome_debug.log", "debug.log"}
KEY_RE = re.compile(r"chrome/(?:chunks|manifests)/[0-9a-f]{32}/[A-Za-z0-9_.-]+\Z")
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        h = sha256()
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(data)
        return h.hexdigest()


def profile_initialized(profile: Path) -> bool:
    """Accept the active Chromium user profile, not only its default name.

    Some signed-in Chromium sessions start in 'Profile 1' rather than
    'Default'. All supported directories are archived/restored by this vault.
    """
    if not (profile / "Local State").is_file():
        return False
    if (profile / "Default").is_dir():
        return True
    try:
        return any(
            entry.is_dir() and not entry.is_symlink()
            and re.fullmatch(r"Profile [1-9][0-9]*", entry.name)
            for entry in profile.iterdir()
        )
    except (OSError, ValueError):
        return False


def _is_empty(profile: Path) -> bool:
    return not profile.exists() or (profile.is_dir() and not any(profile.iterdir()))


class ChromeVault(CloudCheckpoint):
    def _key_bytes(self) -> bytes:
        # Key rotation means old backups require the OLD key. No extra owner
        # passphrase / additional service is needed on this single-user worker.
        return HKDF(algorithm=hashes.SHA256(), length=32,
                    salt=b"money-ai-owned-chrome-profile-v1",
                    info=AAD).derive(self._key.encode("utf-8"))

    def _path(self, key: str) -> str:
        if not isinstance(key, str) or not KEY_RE.fullmatch(key):
            raise CheckpointError("CHROME_VAULT_INVALID_OBJECT_KEY")
        from urllib.parse import quote
        return f"/storage/v1/object/{BUCKET}/" + quote(key, safe="/")

    def latest(self) -> dict | None:
        answer = self._request("GET", TABLE + "?name=eq.owner&select=manifest_path,manifest_sha256,byte_size")
        try:
            rows = answer.json()
        except (TypeError, ValueError):
            raise CheckpointError("CHROME_VAULT_BAD_INDEX") from None
        if not isinstance(rows, list) or len(rows) > 1:
            raise CheckpointError("CHROME_VAULT_BAD_INDEX")
        return rows[0] if rows else None

    def _put_verified(self, key: str, content: bytes, content_type: str) -> None:
        self._request("POST", self._path(key), payload=content,
                      extra_headers={"Content-Type": content_type, "x-upsert": "false"})
        result = self._request("GET", self._path(key)).content
        if len(result) != len(content) or sha256(result).digest() != sha256(content).digest():
            raise CheckpointError("CHROME_VAULT_UPLOAD_READBACK_MISMATCH")

    @staticmethod
    def _zip_profile(profile: Path, target: Path) -> int:
        if not profile_initialized(profile):
            raise CheckpointError("CHROME_PROFILE_NOT_INITIALIZED")
        total = 0
        count = 0
        # A running Chromium continually writes databases. Never retry a hot
        # SQLite database forever while the owner waits for a Cloud backup.
        deadline = time.monotonic() + 110
        last_report = time.monotonic()
        print("TOOL_SYSTEM_VAULT_STAGE=SNAPSHOT_START", flush=True)
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=1, allowZip64=True) as output:
            for root, dirs, files in os.walk(profile, followlinks=False):
                root_path = Path(root)
                dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS
                                 and not (root_path / x).is_symlink())
                if time.monotonic() > deadline:
                    raise CheckpointError("CHROME_VAULT_SNAPSHOT_TIMEOUT")
                for name in sorted(files):
                    if time.monotonic() > deadline:
                        raise CheckpointError("CHROME_VAULT_SNAPSHOT_TIMEOUT")
                    if time.monotonic() - last_report >= 8:
                        print("TOOL_SYSTEM_VAULT_STAGE=SNAPSHOT_PROGRESS files=" + str(count), flush=True)
                        last_report = time.monotonic()
                    source = root_path / name
                    if (name in SKIP_NAMES or name.endswith(("-wal", "-shm", "-journal", ".lock"))
                            or source.is_symlink() or not source.is_file()):
                        continue
                    rel = source.relative_to(profile).as_posix()
                    # Only profile state, never Chromium's top-level binaries, caches or telemetry.
                    if not (rel == "Local State" or rel.startswith("Default/")
                            or (rel.startswith("Profile ") and "/" in rel)):
                        continue
                    first = source.stat()
                    count += 1
                    total += first.st_size
                    if count > MAX_FILES or total > MAX_EXPANDED:
                        raise CheckpointError("CHROME_PROFILE_TOO_LARGE")
                    try:
                        with source.open("rb") as reader:
                            sqlite_header = reader.read(16) == b"SQLite format 3\x00"
                        if sqlite_header:
                            # Chrome may be running. SQLite backup captures a
                            # consistent WAL-aware database without copying -wal.
                            with tempfile.TemporaryDirectory(prefix="chrome-db-") as tempdir:
                                snap = Path(tempdir) / "db.sqlite"
                                sqlite_deadline = min(deadline, time.monotonic() + 22)
                                def _bound_sqlite(_status, _remaining, _total):
                                    if time.monotonic() > sqlite_deadline:
                                        raise CheckpointError("CHROME_VAULT_SQLITE_SNAPSHOT_TIMEOUT")
                                with sqlite3.connect(f"file:{source}?mode=ro", uri=True,
                                                     timeout=2) as db:
                                    with sqlite3.connect(snap) as dest:
                                        db.backup(dest, pages=64, progress=_bound_sqlite, sleep=0.05)
                                output.write(snap, rel)
                        else:
                            output.write(source, rel)
                            after = source.stat()
                            if (first.st_size, first.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                                raise CheckpointError("CHROME_PROFILE_CHANGED_DURING_SNAPSHOT")
                    except (sqlite3.DatabaseError, OSError, PermissionError):
                        raise CheckpointError("CHROME_PROFILE_SNAPSHOT_IO_FAILED") from None
        if target.stat().st_size > MAX_ARCHIVE:
            raise CheckpointError("CHROME_ARCHIVE_EXCEEDS_CAP")
        print("TOOL_SYSTEM_VAULT_STAGE=SNAPSHOT_DONE files=" + str(count) +
              " archive_bytes=" + str(target.stat().st_size), flush=True)
        return count

    def _encrypt(self, source: Path, destination: Path) -> None:
        nonce = os.urandom(12)
        encryptor = Cipher(algorithms.AES(self._key_bytes()), modes.GCM(nonce)).encryptor()
        encryptor.authenticate_additional_data(AAD)
        with source.open("rb") as src, destination.open("wb") as dest:
            dest.write(HEADER + nonce)
            for data in iter(lambda: src.read(1024 * 1024), b""):
                dest.write(encryptor.update(data))
            dest.write(encryptor.finalize())
            dest.write(encryptor.tag)

    def _decrypt(self, source: Path, destination: Path) -> None:
        with source.open("rb") as stream:
            if stream.read(len(HEADER)) != HEADER:
                raise CheckpointError("CHROME_VAULT_BAD_ENCRYPTION_HEADER")
            nonce = stream.read(12)
            stream.seek(-16, os.SEEK_END)
            tag = stream.read(16)
            cipher_length = source.stat().st_size - len(HEADER) - 12 - 16
            if cipher_length < 0 or cipher_length > MAX_ARCHIVE + 1024:
                raise CheckpointError("CHROME_VAULT_INVALID_CIPHER_LENGTH")
            decryptor = Cipher(algorithms.AES(self._key_bytes()), modes.GCM(nonce, tag)).decryptor()
            decryptor.authenticate_additional_data(AAD)
            stream.seek(len(HEADER) + 12)
            try:
                with destination.open("wb") as out:
                    while cipher_length:
                        block = stream.read(min(1024 * 1024, cipher_length))
                        if not block:
                            raise CheckpointError("CHROME_VAULT_TRUNCATED_CIPHER")
                        out.write(decryptor.update(block))
                        cipher_length -= len(block)
                    out.write(decryptor.finalize())
            except Exception as exc:
                if isinstance(exc, CheckpointError):
                    raise
                raise CheckpointError("CHROME_VAULT_DECRYPT_FAILED") from None

    def backup(self, profile: Path | str, *, snapshot_context=None) -> str:
        """Verify one immutable backup; the optional context stops Chrome ONLY for ZIP.

        The owner-visible Chrome is resumed before AES encryption and network
        upload, even if ZIP snapshot fails. Never interrupt it during upload.
        """
        profile = Path(profile)
        with tempfile.TemporaryDirectory(prefix="money-ai-chrome-vault-") as tmp:
            root = Path(tmp)
            archive, sealed = root / "chrome.zip", root / "sealed.bin"
            with (snapshot_context() if snapshot_context is not None else nullcontext()):
                count = self._zip_profile(profile, archive)
            print("TOOL_SYSTEM_VAULT_STAGE=CHROME_RESUMED_BEFORE_CLOUD", flush=True)
            archive_hash = _hash(archive)
            print("TOOL_SYSTEM_VAULT_STAGE=ENCRYPT", flush=True)
            self._encrypt(archive, sealed)
            sealed_hash = _hash(sealed)
            print("TOOL_SYSTEM_VAULT_STAGE=CLOUD_UPLOAD_START bytes=" +
                  str(sealed.stat().st_size), flush=True)
            snapshot = uuid.uuid4().hex
            chunks = []
            with sealed.open("rb") as stream:
                i = 0
                while piece := stream.read(CHUNK_SIZE):
                    key = f"chrome/chunks/{snapshot}/{i:04d}.bin"
                    print("TOOL_SYSTEM_VAULT_STAGE=CLOUD_CHUNK_START number=" + str(i + 1), flush=True)
                    self._put_verified(key, piece, "application/octet-stream")
                    print("TOOL_SYSTEM_VAULT_STAGE=CLOUD_CHUNK_VERIFIED number=" + str(i + 1), flush=True)
                    chunks.append({"key": key, "sha256": sha256(piece).hexdigest(), "size": len(piece)})
                    i += 1
            manifest = {"version": 1, "archive_sha256": archive_hash,
                        "sealed_sha256": sealed_hash, "sealed_bytes": sealed.stat().st_size,
                        "count": count, "chunks": chunks}
            data = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            key = f"chrome/manifests/{snapshot}/manifest.json"
            print("TOOL_SYSTEM_VAULT_STAGE=MANIFEST_VERIFY", flush=True)
            self._put_verified(key, data, "application/octet-stream")
            print("TOOL_SYSTEM_VAULT_STAGE=COMMIT_INDEX", flush=True)
            self._request("POST", TABLE + "?on_conflict=name",
                          json_body={"name": "owner", "manifest_path": key,
                                     "manifest_sha256": sha256(data).hexdigest(), "byte_size": len(data)},
                          extra_headers={"Content-Type": "application/json",
                                         "Prefer": "resolution=merge-duplicates,return=minimal"})
            print("TOOL_SYSTEM_VAULT_STAGE=COMMITTED", flush=True)
            return archive_hash

    @staticmethod
    def _safe_unpack(source: Path, destination: Path) -> None:
        with zipfile.ZipFile(source) as zipped:
            entries = zipped.infolist()
            total = 0
            if len(entries) > MAX_FILES:
                raise CheckpointError("CHROME_VAULT_TOO_MANY_FILES")
            for info in entries:
                rel = PurePosixPath(info.filename)
                allowed = (info.filename == "Local State" or info.filename.startswith("Default/")
                           or (info.filename.startswith("Profile ") and "/" in info.filename))
                if (not info.filename or rel.is_absolute() or ".." in rel.parts
                        or "\\" in info.filename or not allowed):
                    raise CheckpointError("CHROME_VAULT_UNSAFE_ZIP_ENTRY")
                file_kind = stat.S_IFMT(info.external_attr >> 16)
                if info.is_dir() or file_kind not in (0, stat.S_IFREG):
                    raise CheckpointError("CHROME_VAULT_UNSAFE_ZIP_TYPE")
                total += info.file_size
                if total > MAX_EXPANDED:
                    raise CheckpointError("CHROME_VAULT_EXPANDED_CAP")
                target = destination.joinpath(*rel.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                # Chrome's cookie and token databases must not become
                # world-readable after extraction, regardless of ZIP metadata.
                parent = target.parent
                while parent != destination.parent:
                    if parent.is_relative_to(destination):
                        parent.chmod(0o700)
                    parent = parent.parent
                with zipped.open(info) as inp, target.open("xb") as out:
                    shutil.copyfileobj(inp, out, length=1024 * 1024)
                target.chmod(0o600)
        if not profile_initialized(destination):
            raise CheckpointError("CHROME_VAULT_RESTORED_PROFILE_INVALID")

    def restore(self, profile: str | Path) -> bool:
        """Restore only when destination is absent/empty AND Chrome is stopped."""
        profile = Path(profile)
        if not _is_empty(profile):
            raise CheckpointError("CHROME_VAULT_REFUSE_OVERWRITE_PROFILE")
        pointer = self.latest()
        if pointer is None:
            return False
        key = pointer.get("manifest_path")
        digest = pointer.get("manifest_sha256")
        size = pointer.get("byte_size")
        if (not isinstance(key, str) or not key.startswith("chrome/manifests/")
                or not isinstance(digest, str) or not SHA_RE.fullmatch(digest)
                or not isinstance(size, int) or not 0 < size < 256 * 1024):
            raise CheckpointError("CHROME_VAULT_BAD_POINTER")
        manifest_bytes = self._request("GET", self._path(key)).content
        if len(manifest_bytes) != size or sha256(manifest_bytes).hexdigest() != digest:
            raise CheckpointError("CHROME_VAULT_MANIFEST_HASH_MISMATCH")
        try:
            m = json.loads(manifest_bytes)
            chunks = m["chunks"]
            assert m["version"] == 1 and isinstance(chunks, list) and 0 < len(chunks) <= 20
            assert SHA_RE.fullmatch(m["archive_sha256"]) and SHA_RE.fullmatch(m["sealed_sha256"])
            assert isinstance(m["sealed_bytes"], int) and 0 < m["sealed_bytes"] <= MAX_ARCHIVE + 1024
        except (ValueError, TypeError, KeyError, AssertionError):
            raise CheckpointError("CHROME_VAULT_BAD_MANIFEST") from None
        profile.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="money-ai-restore-", dir=profile.parent) as temp:
            root = Path(temp)
            sealed, archive = root / "sealed.bin", root / "chrome.zip"
            written = 0
            with sealed.open("wb") as output:
                for idx, item in enumerate(chunks):
                    if not isinstance(item, dict):
                        raise CheckpointError("CHROME_VAULT_BAD_CHUNK")
                    k, h, n = item.get("key"), item.get("sha256"), item.get("size")
                    if (not isinstance(k, str) or not k.startswith("chrome/chunks/")
                            or not isinstance(h, str) or not SHA_RE.fullmatch(h)
                            or not isinstance(n, int) or not 0 < n <= CHUNK_SIZE
                            or not k.endswith(f"/{idx:04d}.bin")):
                        raise CheckpointError("CHROME_VAULT_BAD_CHUNK")
                    blob = self._request("GET", self._path(k)).content
                    if len(blob) != n or sha256(blob).hexdigest() != h:
                        raise CheckpointError("CHROME_VAULT_CHUNK_HASH_MISMATCH")
                    output.write(blob)
                    written += n
                    if written > MAX_ARCHIVE + 1024:
                        raise CheckpointError("CHROME_VAULT_TOO_LARGE")
            if written != m["sealed_bytes"] or _hash(sealed) != m["sealed_sha256"]:
                raise CheckpointError("CHROME_VAULT_SEALED_HASH_MISMATCH")
            self._decrypt(sealed, archive)
            if _hash(archive) != m["archive_sha256"]:
                raise CheckpointError("CHROME_VAULT_ARCHIVE_HASH_MISMATCH")
            stage = root / "restored_profile"
            stage.mkdir()
            self._safe_unpack(archive, stage)
            if not _is_empty(profile):
                raise CheckpointError("CHROME_VAULT_REFUSE_OVERWRITE_PROFILE")
            if profile.exists():
                profile.rmdir()  # Only the empty, stopped destination is allowed.
            os.replace(stage, profile)
            return True
