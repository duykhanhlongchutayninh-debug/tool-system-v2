"""Evidence-gated tools for advancing an existing Tool System V2 job.

This adapter reuses the single authenticated Chrome on CDP 9222, the existing
Python DOM operator, the current MP4 probe, Supabase media archive and Telegram
owner gate. It does not create browsers or bypass login. Facebook posting stays
disabled until submit/reconciliation can prove exactly one resulting permalink.
"""
from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlparse

from .agent import ScriptResult, VideoResult
from .chrome import Chrome
from .core import GateError, Job, Store, digest_file
from .gemini_web import screenplay_from_mp4
from .video import minimum_video_duration, probe_mp4
from .telegram import Telegram

PROFILE = Path("/kaggle/working/tool-system-v2-runtime/chrome_profile")
DOWNLOADS = Path("/kaggle/working/tool-system-v2-runtime/downloads")
FLOW_URL = "https://labs.google/fx/tools/flow"
META_URL = "https://www.meta.ai/create"
_ALLOWED_VIDEO_HOSTS = {"labs.google", "labs.google.com", "flow.google",
                         "flow.google.com", "meta.ai", "www.meta.ai"}


def selected_video_provider(env=None) -> tuple[str, str]:
    env = os.environ if env is None else env
    value = str(env.get("TOOL_SYSTEM_VIDEO_PROVIDER", "flow")).strip().lower()
    if value == "flow":
        return "Google Flow", FLOW_URL
    if value in {"meta", "meta.ai", "meta_ai"}:
        return "Meta AI", META_URL
    raise GateError("VIDEO_PROVIDER_MUST_BE_FLOW_OR_META_AI")


class ProductionTools:
    """Tools compatible with Agent; every successful stage requires evidence."""

    def __init__(self, store: Store, *, env=None, browser_factory=None,
                 screenplay_fn=None, telegram_factory=None, media=None,
                 meta_video_fn=None):
        self.store = store
        self.env = os.environ if env is None else env
        self.browser_factory = browser_factory or Chrome
        self.screenplay_fn = screenplay_fn or screenplay_from_mp4
        self.telegram_factory = telegram_factory or Telegram
        self.media = media
        self.meta_video_fn = meta_video_fn

    def collect(self, job: Job):
        # Step05 has its own source-evidence adapter. Do not silently re-crawl.
        raise GateError("SOURCE_STEP05_ADAPTER_REQUIRED")

    def screenplay(self, job: Job) -> ScriptResult:
        if job.stage not in {"SOURCE_READY", "REVISION_REQUESTED"}:
            raise GateError("SCREENPLAY_REQUIRES_VERIFIED_SOURCE")
        if not job.source_path or not job.source_sha:
            raise GateError("SCREENPLAY_SOURCE_EVIDENCE_MISSING")
        if digest_file(job.source_path) != job.source_sha:
            raise GateError("SCREENPLAY_SOURCE_SHA_MISMATCH")
        result = self.screenplay_fn(job, lessons=self.store.lessons(),
                                    remember=self.store.remember_fix)
        if not isinstance(result, ScriptResult) or not result.screenplay.strip():
            raise GateError("SCREENPLAY_RESULT_INVALID")
        return result

    def generate(self, job: Job) -> VideoResult:
        if job.stage != "SCRIPT_READY" or not job.script or not job.script_sha:
            raise GateError("VIDEO_GENERATION_REQUIRES_SCRIPT_READY")
        if sha256(job.script.encode("utf-8")).hexdigest() != job.script_sha:
            raise GateError("VIDEO_SCRIPT_SHA_MISMATCH")
        provider, start_url = selected_video_provider(self.env)
        goal = (
            "Create and download a real finished MP4 video using this website. "
            "Do not merely describe a plan or return text. Do not publish or share it. "
            "Use the supplied script faithfully; vertical 9:16 format, at least 30 seconds. "
            "If the site cannot create the video or download the completed MP4, stop and fail. "
            "If login, CAPTCHA, payment, new terms or human confirmation is required, stop. "
            "Do not claim completion without an observed downloaded file.\n\n"
            "SCRIPT SHA256: " + job.script_sha + "\nSCRIPT:\n" + job.script[:12000]
        )
        if provider == "Meta AI":
            # Reuse the existing Chrome context and the previously verified Meta AI
            # /create UI: exact composer -> one Send -> /prompt UUID -> MP4 download.
            from .meta_ai_web import create_video_from_script
            create_video = self.meta_video_fn or create_video_from_script
            result = create_video(job, browser_factory=self.browser_factory)
            result_path = str(result.path)
            result_page_url = result.page_url
        else:
            with self.browser_factory(
                profile_dir=PROFILE, downloads_dir=DOWNLOADS,
                headless=False, cdp_url="http://127.0.0.1:9222",
            ) as browser:
                result = browser.run(start_url, goal, max_steps=120,
                                     lessons=self.store.lessons())
            result_path = result.download_path
            result_page_url = result.page_url
        host = (urlparse(result_page_url).hostname or "").lower()
        if host not in _ALLOWED_VIDEO_HOSTS:
            raise GateError("VIDEO_PROVIDER_NAVIGATED_OUTSIDE_APPROVED_DOMAIN")
        if not result_path:
            raise GateError("VIDEO_MP4_DOWNLOAD_NOT_OBSERVED")
        path = Path(result_path)
        return self._verify_and_archive_video(job, path, provider=provider)

    def verify_and_archive_reconciled_video(self, job: Job, path: str | Path, *,
                                            provider: str = "Meta AI") -> VideoResult:
        """Validate/archive an MP4 recovered from the exact prior Meta UI request."""
        return self._verify_and_archive_video(job, Path(path), provider=provider)

    def _verify_and_archive_video(self, job: Job, path: Path, *,
                                  provider: str) -> VideoResult:
        if job.stage != "SCRIPT_READY" or not job.script or not job.script_sha:
            raise GateError("VIDEO_GENERATION_REQUIRES_SCRIPT_READY")
        if not path.is_file() or path.suffix.lower() != ".mp4":
            raise GateError("VIDEO_DOWNLOAD_IS_NOT_MP4")
        minimum = minimum_video_duration(provider)
        print(
            "TOOL_SYSTEM_VIDEO_QA_PROVIDER=" + provider.replace(" ", "_")
            + ";minimum_duration_seconds=" + f"{minimum:.1f}",
            flush=True,
        )
        proof = probe_mp4(path, min_duration=minimum)
        if sha256(job.script.encode("utf-8")).hexdigest() != job.script_sha:
            raise GateError("VIDEO_SCRIPT_CHANGED_DURING_GENERATION")
        digest = digest_file(path)
        if self.media is None:
            from .cloud_media import CloudMedia
            media = CloudMedia()
        else:
            media = self.media
        media.upload_mp4(job.id, path, digest)
        print("TOOL_SYSTEM_VIDEO_ARTIFACT=MP4_PROBED", flush=True)
        print("TOOL_SYSTEM_VIDEO_SHA256=" + digest, flush=True)
        return VideoResult(path=path, provider=provider, duration=proof.duration,
                           width=proof.width, height=proof.height)

    def _telegram(self):
        token = (self.env.get("TOOL_SYSTEM_TELEGRAM_BOT_TOKEN") or
                 self.env.get("MONEY_TELEGRAM_BOT_TOKEN") or "")
        owner = (self.env.get("TOOL_SYSTEM_TELEGRAM_OWNER_CHAT_ID") or
                 self.env.get("TOOL_SYSTEM_TELEGRAM_OWNER_ID") or
                 self.env.get("MONEY_TELEGRAM_OWNER_CHAT_ID") or "")
        if not token or not str(owner).isdigit():
            raise GateError("TELEGRAM_OWNER_CONFIGURATION_MISSING")
        return self.telegram_factory(token, int(owner), self.store)

    def send_for_review(self, job: Job) -> str:
        if job.stage != "VIDEO_READY" or not job.video_path or not job.video_sha:
            raise GateError("TELEGRAM_REVIEW_REQUIRES_VERIFIED_VIDEO")
        if digest_file(job.video_path) != job.video_sha:
            raise GateError("TELEGRAM_REVIEW_VIDEO_SHA_MISMATCH")
        receipt = self._telegram().send_for_review(job)
        if not str(receipt).isdigit():
            raise GateError("TELEGRAM_REVIEW_RECEIPT_INVALID")
        return str(receipt)

    def publish(self, job: Job) -> str:
        # Do not let a generic visual planner click an uncertain Facebook submit.
        # Implement only with Page-specific reconciliation after ambiguous outcomes.
        raise GateError("FACEBOOK_PUBLISH_RECONCILIATION_NOT_IMPLEMENTED")

    def error(self, job: Job, message: str) -> None:
        self._telegram().error(job, message)
