"""Step 08 browser-only Facebook publishing, controlled by an AI brain.

The only browser is the existing Chrome exposed at CDP 9222. No Facebook
Graph API or Meta publishing endpoint is used. All irreversible actions require
a durable pre-submit record and visible post-action verification.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
import json
import re
import unicodedata

from .browser_brain import BrowserBrain, BrowserDecision
from .core import Job, digest_file

PAGE_NAME = "Mẹo nhỏ mỗi ngày"
PAGE_ID = "1292349630639078"
ZONE_NAME = "Asia/Ho_Chi_Minh"
EVENT_ATTEMPT = "FACEBOOK_UI_STEP08_ATTEMPT"
EVENT_UNCERTAIN = "FACEBOOK_UI_STEP08_UNCERTAIN"
EVENT_VERIFIED = "FACEBOOK_UI_STEP08_VERIFIED"
EVENT_BLOCKED = "FACEBOOK_UI_STEP08_BLOCKED"
EVENT_ACTION = "FACEBOOK_UI_STEP08_ACTION"
MAX_ACTIONS = 20
MAX_RUN_SECONDS = 1200
TAG = "data-money-ai-step08-id"
COMMIT_WORDS = re.compile(
    r"\b(publish|post|share)(?:\s+now)?\b|\bschedule\b|đăng|lên lịch|chia sẻ",
    re.I,
)
PUBLISH_WORDS = re.compile(r"\b(publish|post|share)(?:\s+now)?\b|đăng|chia sẻ", re.I)
SCHEDULE_WORDS = re.compile(r"\bschedule\b|lên lịch", re.I)
PUBLISHED_WORDS = re.compile(
    r"(published|your reel is live|post is live|successfully posted|đã đăng|đã được đăng|đã chia sẻ)",
    re.I,
)
SCHEDULED_WORDS = re.compile(r"(scheduled|scheduled for|đã lên lịch|được lên lịch|lịch đăng)", re.I)
URL_WORDS = re.compile(r"^https://(?:www\.)?facebook\.com/", re.I)
SHA_RE = re.compile(r"^[a-f0-9]{64}$")
JOB_RE = re.compile(r"^[a-f0-9]{16}$")

OBSERVE_JS = r"""() => {
  const selector = 'button,a,input,textarea,select,[role="button"],[role="link"],' +
    '[role="menuitem"],[role="combobox"],[role="textbox"],[contenteditable="true"],[tabindex]';
  const all = Array.from(document.querySelectorAll(selector));
  const controls = [];
  for (const e of all) {
    const tag = (e.tagName || '').toLowerCase();
    const type = (e.getAttribute('type') || '').toLowerCase();
    const r = e.getBoundingClientRect();
    const cs = getComputedStyle(e);
    const visible = r.width > 0 && r.height > 0 && r.bottom > 0 &&
      r.top < innerHeight && r.right > 0 && r.left < innerWidth &&
      cs.visibility !== 'hidden' && cs.display !== 'none';
    const fileInput = tag === 'input' && type === 'file';
    if (!visible && !fileInput) continue;
    if (tag === 'input' && ['password','hidden'].includes(type)) continue;
    if (controls.length >= 100) break;
    const id = String(controls.length);
    e.setAttribute('data-money-ai-step08-id', id);
    const text = (e.getAttribute('aria-label') || e.getAttribute('title') ||
      e.getAttribute('placeholder') || e.innerText || e.value || e.getAttribute('alt') || '')
      .replace(/\s+/g, ' ').trim().slice(0, 160);
    const href = tag === 'a' ? (e.getAttribute('href') || '').slice(0, 180) : '';
    controls.push({
      id, tag, type, role: e.getAttribute('role') || '',
      text, href, visible, disabled: !!e.disabled ||
        e.getAttribute('aria-disabled') === 'true',
      editable: !!e.isContentEditable || ['textarea'].includes(tag) ||
        (tag === 'input' && ['text','search','date','time','datetime-local','number'].includes(type)),
      value: type === 'password' ? '' : String(e.value || e.innerText || '').slice(0, 140)
    });
  }
  const body = (document.body?.innerText || '').replace(/\u00a0/g, ' ').slice(0, 6500);
  const videos = Array.from(document.querySelectorAll('video')).some(v => {
    const r = v.getBoundingClientRect(); return r.width > 100 && r.height > 80;
  });
  const fileReady = Array.from(document.querySelectorAll('input[type="file"]'))
    .some(e => e.files && e.files.length > 0);
  const editableText = Array.from(document.querySelectorAll(
    '[contenteditable="true"],textarea,input[type="text"],input[type="search"]'))
    .filter(e => {const r=e.getBoundingClientRect(); return r.width>0 && r.height>0;})
    .map(e => String(e.value || e.innerText || '').slice(0, 12000)).slice(0, 20);
  return {url: location.href.slice(0, 500), title: document.title.slice(0, 200),
    body, controls, videos, fileReady, editableText};
}"""


def _norm(value: object) -> str:
    return " ".join(unicodedata.normalize("NFC", str(value or "")).split()).casefold()


def next_schedule_at(now: datetime | None = None) -> datetime:
    zone = ZoneInfo(ZONE_NAME)
    current = (now or datetime.now(zone)).astimezone(zone)
    target = current.replace(hour=7, minute=30, second=0, microsecond=0)
    if target <= current + timedelta(minutes=20):
        target += timedelta(days=1)
    return target


def _event_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    try:
        value = json.loads(str(event.get("details") or "{}"))
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def prior_mode_state(events: list[dict[str, Any]], mode: str, video_sha: str) -> str:
    """Return NEW/VERIFIED/UNCERTAIN; quarantine legacy API attempts for UI reconciliation."""
    attempts = []
    verified = False
    legacy_schedule = None
    legacy_publish = None
    for event in events:
        kind = event.get("kind")
        data = _event_payload(event)
        if not data:
            continue
        if kind in {EVENT_ATTEMPT, EVENT_UNCERTAIN, EVENT_VERIFIED}:
            if data.get("mode") != mode or data.get("video_sha") != video_sha:
                continue
            if kind == EVENT_VERIFIED and data.get("state") == "VERIFIED":
                verified = True
            elif kind in {EVENT_ATTEMPT, EVENT_UNCERTAIN}:
                attempts.append(event)
        elif data.get("video_sha") == video_sha and data.get("page_id") == PAGE_ID:
            # Keep legacy receipts visible during migration so the browser UI
            # won't blindly create another post for the same approved MP4.
            if mode == "schedule" and kind == "FACEBOOK_REEL_SCHEDULE_ATTEMPT":
                legacy_schedule = data
            elif mode == "publish" and kind == "FACEBOOK_DRAFT_TEST_ATTEMPT":
                legacy_publish = data

    if verified:
        return "VERIFIED"
    if attempts:
        return "UNCERTAIN"
    if mode == "schedule" and legacy_schedule:
        old_state = str(legacy_schedule.get("state") or "")
        if old_state == "SCHEDULE_START_REJECTED":
            return "NEW"
        return "LEGACY_UNCERTAIN"
    if mode == "publish" and legacy_publish:
        old_state = str(legacy_publish.get("state") or "")
        old_mode = str(legacy_publish.get("mode") or "")
        if old_mode != "PUBLIC_PUBLISH":
            return "NEW"  # A private draft is not a public-post attempt.
        if old_state == "START_REJECTED":
            return "NEW"
        return "LEGACY_UNCERTAIN"
    return "NEW"

def _visible_url_is_facebook(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host == "facebook.com" or host.endswith(".facebook.com")


def _control_text(control: dict[str, Any]) -> str:
    return " ".join(str(control.get(k) or "") for k in ("text", "role", "href")).strip()


class FacebookBrowserBrainAgent:
    def __init__(self, *, page: Any, brain: BrowserBrain, store: Any, job: Job,
                 video_path: Path, mode: str, schedule_at: datetime | None = None) -> None:
        if mode not in {"publish", "schedule"}:
            raise RuntimeError("STEP08_MODE_INVALID")
        self.page, self.brain, self.store, self.job = page, brain, store, job
        self.video_path, self.mode, self.schedule_at = video_path, mode, schedule_at
        self.upload_selected = False
        self.commit_clicked = False
        self.last_action = ""
        self.action_count = 0
        self.started_at = datetime.now().timestamp()
        self.objective = self._make_objective()

    def _make_objective(self) -> str:
        schedule = (self.schedule_at.strftime("%Y-%m-%d %H:%M") + " " + ZONE_NAME
                    if self.schedule_at else "NOT_APPLICABLE")
        return json.dumps({
            "task": "Create one Facebook Reel using the approved asset through the visible Facebook UI.",
            "mode": self.mode,
            "target_page": PAGE_NAME,
            "locked_page_id_if_shown": PAGE_ID,
            "approved_video_filename": self.video_path.name,
            "approved_video_sha256": self.job.video_sha,
            "caption_exact_text": self.job.script or "",
            "schedule_target_local_time": schedule,
            "rules": [
                "Only use the existing signed-in Facebook browser UI; never call a Facebook API.",
                "Do not change the target Page, video, caption, or schedule target.",
                "The user has already approved this exact video SHA in Telegram.",
                "For publish mode, publish once immediately; do not schedule it.",
                "For schedule mode, create a Meta-native scheduled Reel for the exact target time; do not publish immediately.",
                "If login, Page identity, upload readiness, caption or schedule time is unclear, investigate or block.",
                "Do not click the final submit control through a generic click action.",
                "The action executor returns a fresh screen and DOM after each action.",
            ],
        }, ensure_ascii=False, separators=(",", ":"))

    def _observe(self) -> tuple[dict[str, Any], bytes]:
        try:
            state = self.page.evaluate(OBSERVE_JS)
            state["money_ai_context"] = {
                "target_page": PAGE_NAME,
                "mode": self.mode,
                "schedule_time": self.schedule_at.strftime("%Y-%m-%d %H:%M") if self.schedule_at else None,
                "approved_video_sha256": self.job.video_sha,
                "video_upload_action_completed": self.upload_selected,
                "final_submit_already_clicked_this_run": self.commit_clicked,
            }
            screenshot = self.page.screenshot(type="jpeg", quality=58, timeout=15000)
        except Exception as exc:
            raise RuntimeError("STEP08_OBSERVE_FAILED_" + type(exc).__name__.upper()) from None
        if not isinstance(state, dict) or not isinstance(state.get("controls"), list):
            raise RuntimeError("STEP08_OBSERVATION_SHAPE_INVALID")
        return state, screenshot

    def _locate_control(self, state: dict[str, Any], element_id: str) -> tuple[Any, dict[str, Any]]:
        match = next((c for c in state["controls"] if c.get("id") == element_id), None)
        if not match:
            raise RuntimeError("STEP08_BRAIN_TARGET_NOT_IN_FRESH_OBSERVATION")
        selector = '[data-money-ai-step08-id="' + element_id + '"]'
        locator = self.page.locator(selector)
        try:
            count = locator.count()
        except Exception:
            count = 0
        if count != 1:
            raise RuntimeError("STEP08_CONTROL_ID_NOT_UNIQUE")
        return locator, match

    def _target_page_visible(self, state: dict[str, Any]) -> bool:
        return PAGE_NAME in str(state.get("body") or "") and _visible_url_is_facebook(
            str(state.get("url") or "")
        )

    def _upload_is_visibly_ready(self, state: dict[str, Any]) -> bool:
        body = str(state.get("body") or "")
        name = self.video_path.name
        visible_preview = bool(state.get("videos")) or bool(state.get("fileReady")) or name in body
        return self.upload_selected and visible_preview

    def _caption_is_exact(self, state: dict[str, Any]) -> bool:
        expected = _norm(self.job.script or "")
        if len(expected) < 20:
            return False
        editors = state.get("editableText") or []
        return any(_norm(value) == expected for value in editors if isinstance(value, str))

    def _schedule_time_is_visible(self, state: dict[str, Any]) -> bool:
        if self.schedule_at is None:
            return False
        date_value = self.schedule_at.strftime("%Y-%m-%d")
        time_value = self.schedule_at.strftime("%H:%M")
        body = str(state.get("body") or "")
        controls = state.get("controls") or []
        field_values = " ".join(str(c.get("value") or "") for c in controls)
        time_found = time_value in body or time_value in field_values or \
            self.schedule_at.strftime("%-I:%M %p").casefold() in body.casefold()
        date_found = date_value in body or date_value in field_values or \
            self.schedule_at.strftime("%b %-d").casefold() in body.casefold() or \
            self.schedule_at.strftime("%B %-d").casefold() in body.casefold()
        return time_found and date_found

    def _log_action(self, decision: BrowserDecision) -> None:
        details = json.dumps({
            "mode": self.mode, "video_sha": self.job.video_sha,
            "action": decision.action, "element_id": decision.element_id,
            "action_number": self.action_count,
        }, sort_keys=True)
        self.store.record_event(self.job.id, EVENT_ACTION, details)

    def _submit(self, decision: BrowserDecision, state: dict[str, Any]) -> None:
        if self.commit_clicked:
            raise RuntimeError("STEP08_DUPLICATE_SUBMIT_BLOCKED")
        wanted = "publish_now" if self.mode == "publish" else "schedule_post"
        if decision.action != wanted:
            raise RuntimeError("STEP08_SUBMIT_MODE_MISMATCH")
        if not self._target_page_visible(state):
            raise RuntimeError("STEP08_TARGET_PAGE_IDENTITY_NOT_VISIBLE")
        if not self._upload_is_visibly_ready(state):
            raise RuntimeError("STEP08_APPROVED_VIDEO_PREVIEW_NOT_VERIFIED")
        if not self._caption_is_exact(state):
            raise RuntimeError("STEP08_APPROVED_CAPTION_NOT_VERIFIED")
        if self.mode == "schedule" and not self._schedule_time_is_visible(state):
            raise RuntimeError("STEP08_SCHEDULE_TIME_NOT_VERIFIED")
        locator, control = self._locate_control(state, str(decision.element_id))
        label = _control_text(control)
        if self.mode == "publish" and not PUBLISH_WORDS.search(label):
            raise RuntimeError("STEP08_PUBLISH_FINAL_CONTROL_NOT_MATCHED")
        if self.mode == "schedule" and not SCHEDULE_WORDS.search(label):
            raise RuntimeError("STEP08_SCHEDULE_FINAL_CONTROL_NOT_MATCHED")
        if control.get("disabled") or not control.get("visible"):
            raise RuntimeError("STEP08_FINAL_CONTROL_NOT_ACTIONABLE")
        intent = {
            "mode": self.mode, "video_sha": self.job.video_sha,
            "page": PAGE_NAME, "state": "SUBMIT_INTENT",
            "schedule_at": self.schedule_at.isoformat() if self.schedule_at else None,
            "at": datetime.now(ZoneInfo(ZONE_NAME)).isoformat(),
        }
        # This durable receipt must succeed before an irreversible Facebook click.
        self.store.record_event(self.job.id, EVENT_ATTEMPT, json.dumps(intent, sort_keys=True))
        self.commit_clicked = True
        try:
            locator.click(timeout=12000)
        except Exception as exc:
            # We have persisted intent; a timeout may occur after the click reached Facebook.
            raise RuntimeError("STEP08_SUBMIT_OUTCOME_UNCERTAIN_" + type(exc).__name__.upper()) from None
        self.store.record_event(self.job.id, EVENT_UNCERTAIN, json.dumps({
            **intent, "state": "CLICK_SENT_RESULT_NOT_YET_VERIFIED",
        }, sort_keys=True))
        self.page.wait_for_timeout(1500)

    def _verify_complete(self, decision: BrowserDecision, state: dict[str, Any]) -> dict[str, Any]:
        if not self.commit_clicked:
            raise RuntimeError("STEP08_BRAIN_CLAIMED_SUCCESS_WITHOUT_SUBMIT")
        body = str(state.get("body") or "")
        evidence = (decision.evidence or "").strip()
        if not evidence or evidence not in body:
            raise RuntimeError("STEP08_SUCCESS_EVIDENCE_NOT_VISIBLE")
        if not self._target_page_visible(state):
            raise RuntimeError("STEP08_SUCCESS_PAGE_IDENTITY_NOT_VISIBLE")
        if self.mode == "publish":
            if not PUBLISHED_WORDS.search(body):
                raise RuntimeError("STEP08_PUBLISH_SUCCESS_TEXT_NOT_VISIBLE")
        else:
            if not SCHEDULED_WORDS.search(body) or not self._schedule_time_is_visible(state):
                raise RuntimeError("STEP08_SCHEDULE_SUCCESS_OR_TIME_NOT_VISIBLE")
        try:
            links = self.page.eval_on_selector_all(
                'a[href^="https://www.facebook.com/"]',
                "(items) => items.map(a => a.href).filter(h => /\\/(reel|posts|videos)\\//i.test(h)).slice(0, 20)"
            )
        except Exception:
            links = []
        permalink = next((h for h in links if isinstance(h, str) and URL_WORDS.match(h)), None)
        if not permalink:
            current = str(state.get("url") or "")
            permalink = current if URL_WORDS.match(current) and re.search(
                r"/(reel|posts|videos)/", current, re.I
            ) else None
        verified = {
            "mode": self.mode, "video_sha": self.job.video_sha,
            "page": PAGE_NAME, "state": "VERIFIED", "evidence": evidence[:240],
            "permalink": permalink,
            "schedule_at": self.schedule_at.isoformat() if self.schedule_at else None,
            "verified_at": datetime.now(ZoneInfo(ZONE_NAME)).isoformat(),
            "model": self.brain.model, "model_calls": self.brain.calls,
            "actions": self.action_count,
        }
        self.store.record_event(self.job.id, EVENT_VERIFIED, json.dumps(verified, sort_keys=True))
        return verified

    def _perform(self, decision: BrowserDecision, state: dict[str, Any]) -> dict[str, Any] | None:
        action = decision.action
        self._log_action(decision)
        if action == "blocked":
            raise RuntimeError("STEP08_BRAIN_BLOCKED")
        if action == "complete":
            return self._verify_complete(decision, state)
        if action in {"publish_now", "schedule_post"}:
            self._submit(decision, state)
            return None
        if action == "wait":
            milliseconds = int(str(decision.value))
            if not 500 <= milliseconds <= 5000:
                raise RuntimeError("STEP08_WAIT_DURATION_OUT_OF_RANGE")
            self.page.wait_for_timeout(milliseconds)
            return None
        if action == "press":
            key = str(decision.value)
            if key not in {"Enter", "Tab", "Escape", "ArrowDown", "ArrowUp",
                           "Control+A", "Backspace", "Delete", "Home", "End"}:
                raise RuntimeError("STEP08_KEY_NOT_ALLOWED")
        locator, control = self._locate_control(state, str(decision.element_id))
        label = _control_text(control)
        if action == "click":
            if COMMIT_WORDS.search(label):
                raise RuntimeError("STEP08_FINAL_CONTROL_REQUIRES_EXPLICIT_COMMIT_ACTION")
            if control.get("disabled") or not control.get("visible"):
                raise RuntimeError("STEP08_CONTROL_NOT_ACTIONABLE")
            locator.click(timeout=10000)
        elif action == "fill":
            if not control.get("editable") or control.get("type") in {"password", "file", "hidden"}:
                raise RuntimeError("STEP08_FILL_TARGET_NOT_EDITABLE")
            fill_value = str(decision.value)
            if fill_value == "[[APPROVED_CAPTION]]":
                fill_value = str(self.job.script or "")
            elif fill_value == "[[SCHEDULE_DATE]]":
                if self.schedule_at is None:
                    raise RuntimeError("STEP08_SCHEDULE_DATE_NOT_APPLICABLE")
                fill_value = self.schedule_at.strftime("%Y-%m-%d")
            elif fill_value == "[[SCHEDULE_TIME]]":
                if self.schedule_at is None:
                    raise RuntimeError("STEP08_SCHEDULE_TIME_NOT_APPLICABLE")
                fill_value = self.schedule_at.strftime("%H:%M")
            locator.fill(fill_value, timeout=10000)
        elif action == "upload_video":
            if control.get("tag") != "input" or control.get("type") != "file":
                raise RuntimeError("STEP08_UPLOAD_TARGET_NOT_FILE_INPUT")
            if self.upload_selected:
                raise RuntimeError("STEP08_VIDEO_UPLOAD_ALREADY_SELECTED")
            locator.set_input_files(str(self.video_path), timeout=15000)
            self.upload_selected = True
            self.page.wait_for_timeout(1000)
        elif action == "select_option":
            if control.get("tag") != "select":
                # A custom dropdown needs a separate click and fresh observation.
                locator.click(timeout=10000)
                return None
            locator.select_option(label=str(decision.value), timeout=10000)
        elif action == "press":
            locator.press(str(decision.value), timeout=10000)
        else:
            raise RuntimeError("STEP08_ACTION_NOT_SUPPORTED")
        self.last_action = action
        return None

    def run(self) -> dict[str, Any]:
        if not _visible_url_is_facebook(str(self.page.url)):
            raise RuntimeError("STEP08_NON_FACEBOOK_PAGE_REFUSED")
        for _ in range(MAX_ACTIONS):
            if datetime.now().timestamp() - self.started_at > MAX_RUN_SECONDS:
                raise RuntimeError("STEP08_MODEL_RUN_DEADLINE")
            state, screenshot = self._observe()
            if self.page.url.rstrip("/") in {"https://www.facebook.com/login", "https://www.facebook.com/checkpoint"}:
                raise RuntimeError("STEP08_FACEBOOK_LOGIN_OR_CHECKPOINT_REQUIRED")
            decision = self.brain.decide(objective=self.objective, state=state, screenshot=screenshot)
            self.action_count += 1
            print("TOOL_SYSTEM_STEP08_BRAIN_ACTION=" + decision.action.upper(), flush=True)
            if decision.action == "blocked":
                print("TOOL_SYSTEM_STEP08_BRAIN_BLOCK_REASON=" +
                      re.sub(r"[^A-Za-z0-9_-]", "_", decision.reason.upper())[:80],
                      flush=True)
            result = self._perform(decision, state)
            if result:
                return result
        raise RuntimeError("STEP08_MAX_BRAIN_ACTIONS_EXCEEDED")


def validate_job(job: Job, expected_page: str = PAGE_NAME) -> None:
    if job.page != expected_page:
        raise RuntimeError("STEP08_ACTIVE_JOB_PAGE_MISMATCH")
    if job.stage != "APPROVED":
        raise RuntimeError("STEP08_TELEGRAM_APPROVAL_REQUIRED")
    if not isinstance(job.video_sha, str) or not SHA_RE.fullmatch(job.video_sha):
        raise RuntimeError("STEP08_APPROVED_VIDEO_SHA_MISSING")
    if job.owner_approved_sha != job.video_sha:
        raise RuntimeError("STEP08_APPROVED_VIDEO_SHA_MISMATCH")
    if not isinstance(job.script, str) or len(job.script.strip()) < 20:
        raise RuntimeError("STEP08_APPROVED_CAPTION_MISSING")
    if not JOB_RE.fullmatch(job.id):
        raise RuntimeError("STEP08_JOB_ID_INVALID")
