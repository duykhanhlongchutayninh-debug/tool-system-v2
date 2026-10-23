"""Private Supabase MP4 archive for one Tool System V2 worker.

Save media to content-addressed immutable keys. Never overwrite an existing
local video with different bytes; large files beyond the free bucket's 50 MiB
limit are reported instead of silently truncated or recompressed.
"""
from __future__ import annotations
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote
import os
import re
import tempfile

from .cloud_checkpoint import CloudCheckpoint, CheckpointError

VIDEO_BUCKET = 'money-ai-media'
MAX_VIDEO = 50 * 1024 * 1024
_JOB = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
_KEY = re.compile(r'videos/([A-Za-z0-9_-]{1,64})/([0-9a-f]{64})\.mp4\Z')
_SHA = re.compile(r'[0-9a-f]{64}\Z')


class CloudMedia(CloudCheckpoint):
    @staticmethod
    def _validate_sha(value):
        if not isinstance(value, str) or not _SHA.fullmatch(value):
            raise CheckpointError('INVALID_MEDIA_CHECKSUM')

    def upload_mp4(self, job_id: str, path: str | Path,
                   expected_sha: str) -> str:
        if not _JOB.fullmatch(job_id):
            raise CheckpointError('INVALID_JOB_ID_FOR_MEDIA')
        self._validate_sha(expected_sha)
        source = Path(path)
        if not source.is_file() or source.suffix.lower() != '.mp4':
            raise CheckpointError('MEDIA_FILE_MISSING_OR_NOT_MP4')
        size = source.stat().st_size
        if not 0 < size <= MAX_VIDEO:
            raise CheckpointError('MEDIA_EXCEEDS_50_MIB_LIMIT')
        blob = source.read_bytes()
        if sha256(blob).hexdigest() != expected_sha:
            raise CheckpointError('MEDIA_LOCAL_HASH_MISMATCH')
        key = f'videos/{job_id}/{expected_sha}.mp4'
        url = f'/storage/v1/object/{VIDEO_BUCKET}/' + quote(key, safe='/')
        # Read-before-write avoids needless duplicate POSTs for an immutable key.
        # The connected Supabase project has been observed returning HTTP 400,
        # rather than 404, for a GET of a valid-but-absent object path. Treat a
        # 400 here only as "absence not yet proven": proceed to a create-only POST
        # and require a successful SHA-verified read-back before reporting success.
        try:
            existing = self._request('GET', url)
        except CheckpointError as error:
            if str(error) == 'SUPABASE_HTTP_404':
                print("TOOL_SYSTEM_SUPABASE_MEDIA_PREFLIGHT=ABSENT_HTTP_404", flush=True)
            elif str(error) == 'SUPABASE_HTTP_400':
                print(
                    "TOOL_SYSTEM_SUPABASE_MEDIA_PREFLIGHT=HTTP_400_UNRESOLVED;"
                    "REQUIRES_POST_AND_SHA_READBACK",
                    flush=True,
                )
            else:
                raise
        else:
            if (len(existing.content) != size or
                    sha256(existing.content).hexdigest() != expected_sha):
                raise CheckpointError('MEDIA_REMOTE_HASH_MISMATCH')
            print("TOOL_SYSTEM_SUPABASE_MEDIA_PREFLIGHT=EXISTING_SHA_VERIFIED", flush=True)
            return key
        try:
            self._request('POST', url, payload=blob, extra_headers={
                'Content-Type': 'video/mp4', 'x-upsert': 'false'})
            print("TOOL_SYSTEM_SUPABASE_MEDIA_POST=SUCCESS", flush=True)
        except CheckpointError as error:
            if str(error) not in ('SUPABASE_HTTP_400', 'SUPABASE_HTTP_409'):
                raise
            # Only a matching, readable existing object proves that this
            # error was a duplicate rather than a real API/auth problem.
            try:
                response = self._request('GET', url)
            except CheckpointError:
                raise error from None
            if (len(response.content) != size or
                    sha256(response.content).hexdigest() != expected_sha):
                raise error from None
            print("TOOL_SYSTEM_SUPABASE_MEDIA_DUPLICATE=REMOTE_SHA_VERIFIED", flush=True)
            return key
        response = self._request('GET', url)
        if len(response.content) != size or sha256(response.content).hexdigest() != expected_sha:
            raise CheckpointError('MEDIA_REMOTE_HASH_MISMATCH')
        print("TOOL_SYSTEM_SUPABASE_MEDIA_READBACK=SHA256_VERIFIED", flush=True)
        return key

    def restore_mp4(self, key: str, expected_sha: str,
                    destination: str | Path) -> Path:
        self._validate_sha(expected_sha)
        found = _KEY.fullmatch(key) if isinstance(key,str) else None
        if found is None or found.group(2) != expected_sha:
            raise CheckpointError('INVALID_MEDIA_OBJECT_PATH')
        dest = Path(destination)
        if dest.exists():
            if dest.is_file() and sha256(dest.read_bytes()).hexdigest() == expected_sha:
                return dest
            raise CheckpointError('REFUSE_OVERWRITE_EXISTING_MEDIA')
        response = self._request('GET', f'/storage/v1/object/{VIDEO_BUCKET}/' + quote(key, safe='/'))
        blob = response.content
        if not 0 < len(blob) <= MAX_VIDEO or sha256(blob).hexdigest() != expected_sha:
            raise CheckpointError('MEDIA_REMOTE_HASH_MISMATCH')
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = None
        try:
            with tempfile.NamedTemporaryFile(prefix='.money-ai-media-', suffix='.mp4',
                                             dir=dest.parent, delete=False) as output:
                temp = Path(output.name)
                output.write(blob)
            if dest.exists():
                raise CheckpointError('REFUSE_OVERWRITE_EXISTING_MEDIA')
            os.replace(temp, dest)
            temp = None
            return dest
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
