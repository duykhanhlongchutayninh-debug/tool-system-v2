"""Archive source/final MP4 before reporting a completed pipeline stage.

Wrap one existing Tools implementation. If Supabase upload/verify fails, the
Agent sees an error and must block/notify, never mark the media durable.
"""
from .cloud_media import CloudMedia
from .core import digest_file
from .video import probe_mp4


class ArchivedTools:
    def __init__(self, inner, media: CloudMedia):
        self.inner = inner
        self.media = media

    def collect(self, job):
        result = self.inner.collect(job)
        self.media.upload_mp4(job.id, result.path, digest_file(result.path))
        return result

    def screenplay(self, job):
        return self.inner.screenplay(job)

    def generate(self, job):
        result = self.inner.generate(job)
        probe_mp4(result.path)  # Do not archive video that cannot be validated.
        self.media.upload_mp4(job.id, result.path, digest_file(result.path))
        return result

    def send_for_review(self, job):
        return self.inner.send_for_review(job)

    def publish(self, job):
        return self.inner.publish(job)

    def error(self, job, message):
        return self.inner.error(job, message)
