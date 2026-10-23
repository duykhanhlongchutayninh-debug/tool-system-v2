"""Meta AI video generation through the previously verified /create UI.

Uses the owner's existing Chrome session. One verified Send click is followed
by a /prompt/<UUID> receipt. A video is accepted only when the same thread
contains the job ID/SHA, a visible video, and the Download button.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import re
import time
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .chrome import Chrome
from .core import GateError, Job

META_CREATE_URL = "https://www.meta.ai/create"
PROFILE = Path("/kaggle/working/tool-system-v2-runtime/chrome_profile")
DOWNLOADS = Path("/kaggle/working/tool-system-v2-runtime/downloads")
META_HOSTS = frozenset({"meta.ai", "www.meta.ai"})
COMPOSER_SELECTOR = 'div[contenteditable=true][data-testid=composer-input]'
PROMPT_PATH = re.compile(r"^/prompt/[a-f0-9-]{36}/?$", re.I)
DOWNLOAD_LABEL = re.compile(r"^(?:download|download video|download mp4|save video|tải xuống|tải video|lưu video)$", re.I)
DANGEROUS_LABEL = re.compile(
    r"publish|post now|share|delete|remove|log.?in|sign.?in|logout|payment|checkout|"
    r"subscribe|purchase|accept|agree|consent|đăng bài|đăng ngay|chia sẻ|xóa|xoá|"
    r"đồng ý|chấp nhận|đăng nhập",
    re.I,
)
NON_SEND_CONTROL = re.compile(
    r"image|photo|picture|avatar|voice|microphone|record|attach|upload|camera|"
    r"file|create image|generate image|tạo hình ảnh|tạo ảnh|đính kèm|"
    r"tải lên|giọng nói|ghi âm",
    re.I,
)
FAILURE_TEXT = {
    "METAAI_LIMIT_REACHED": re.compile(
        r"(?:you.ve reached|daily limit|limit reached|quá giới hạn|đạt giới hạn|"
        r"not enough credits|insufficient credits|out of credits|hết tín dụng)",
        re.I,
    ),
    "METAAI_GENERATION_REJECTED": re.compile(
        r"(?:couldn.t generate|unable to generate|generation failed|something went wrong|"
        r"không thể tạo|tạo video thất bại)",
        re.I,
    ),
}


@dataclass(frozen=True)
class MetaWebArtifact:
    path: Path
    page_url: str
    script_sha: str
    job_id: str
    evidence: str


@dataclass(frozen=True)
class MetaWebReconciliation:
    state: str
    artifact: MetaWebArtifact | None = None


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _normalise(text: object) -> str:
    return " ".join(str(text or "").split())


def _meta_prompt_text(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("Prompt must be text")
    normalized = _normalise(text)
    if len(normalized) < 40:
        raise ValueError("Prompt too short")
    return normalized


def _body_text(page) -> str:
    try:
        return _normalise(page.locator("body").inner_text(timeout=10000))
    except Exception:
        return ""


def _read_editor(editor) -> str:
    try:
        return _normalise(editor.inner_text(timeout=4000))
    except TypeError:
        try:
            return _normalise(editor.inner_text())
        except Exception:
            return ""
    except Exception:
        return ""


def _visible_composer_rows(page) -> list[dict]:
    rows = []
    try:
        controls = page.locator(COMPOSER_SELECTOR)
        count = min(controls.count(), 12)
    except Exception:
        return rows
    for index in range(count):
        editor = controls.nth(index)
        try:
            if not editor.is_visible() or not editor.is_enabled():
                continue
            box = editor.bounding_box()
            if not box or float(box["width"]) < 80 or float(box["height"]) < 12:
                continue
            rows.append({"locator": editor, "box": box, "text": _read_editor(editor)})
        except Exception:
            continue
    return rows


def _find_composer(page):
    rows = _visible_composer_rows(page)
    if not rows:
        raise GateError("METAAI_CREATE_COMPOSER_NOT_FOUND")
    if len(rows) != 1:
        raise GateError("METAAI_CREATE_COMPOSER_NOT_UNIQUE")
    print("TOOL_SYSTEM_METAAI_CREATE_COMPOSER=LIVE_SELECTOR_VERIFIED", flush=True)
    return rows[0]["locator"], rows[0]["box"]


def _create_surface_verified(page) -> bool:
    try:
        current = urlparse(page.url)
        return (
            current.scheme == "https"
            and (current.hostname or "").lower() in META_HOSTS
            and (current.path or "").rstrip("/") == "/create"
            and len(_visible_composer_rows(page)) == 1
        )
    except Exception:
        return False


def _page_is_logged_in(page) -> bool:
    if _host(getattr(page, "url", "")) not in META_HOSTS:
        raise GateError("METAAI_UNEXPECTED_REDIRECT")
    path = (urlparse(page.url).path or "").casefold()
    if any(part in path for part in ("/login", "/signin", "/sign-in", "/auth")):
        return False
    try:
        if page.locator('input[type="password"]:visible').count():
            return False
        if page.locator('input[autocomplete="current-password"]:visible').count():
            return False
        body = _body_text(page).casefold()
        login_copy = (
            "log in or sign up" in body
            or "sign in to get started" in body
            or "đăng nhập để bắt đầu" in body
        )
        if login_copy and not _visible_composer_rows(page):
            return False
    except Exception:
        pass
    return True


def _wait_for_create_surface(page, seconds: float = 30.0) -> None:
    deadline = time.monotonic() + max(0.1, float(seconds))
    while time.monotonic() < deadline:
        if _host(page.url) not in META_HOSTS:
            raise GateError("METAAI_UNEXPECTED_REDIRECT")
        if not _page_is_logged_in(page):
            raise GateError("METAAI_OWNER_LOGIN_REQUIRED")
        if _create_surface_verified(page):
            print("TOOL_SYSTEM_METAAI_CREATE_SURFACE=LIVE_UI_VERIFIED", flush=True)
            return
        page.wait_for_timeout(500)
    raise GateError("METAAI_CREATE_SURFACE_NOT_VERIFIED_AFTER_WAIT")


def _select_or_create_meta_create_tab(context):
    """Prefer an existing /create tab; otherwise reuse a Meta tab in this Chrome context."""
    try:
        pages = list(context.pages)
    except Exception:
        pages = []
    meta_pages = []
    for page in reversed(pages):
        try:
            if page.is_closed() or _host(page.url) not in META_HOSTS:
                continue
            meta_pages.append(page)
            if (urlparse(page.url).path or "").rstrip("/") == "/create":
                page.bring_to_front()
                print("TOOL_SYSTEM_METAAI_TAB=REUSED_EXISTING_CREATE_TAB", flush=True)
                return page
        except Exception:
            continue
    page = meta_pages[0] if meta_pages else context.new_page()
    page.bring_to_front()
    if (urlparse(page.url).path or "").rstrip("/") != "/create":
        page.goto(META_CREATE_URL, wait_until="domcontentloaded", timeout=45000)
    print("TOOL_SYSTEM_METAAI_TAB=" +
          ("REUSED_EXISTING_META_TAB" if meta_pages else "NEW_TAB_IN_EXISTING_CHROME_CONTEXT"),
          flush=True)
    return page


def _control_label(element) -> str:
    """Return one primary label, rather than concatenating duplicated ARIA/text."""
    try:
        details = element.evaluate("""el => ({
            aria: el.getAttribute('aria-label') || '',
            title: el.getAttribute('title') || '',
            text: (el.innerText || el.textContent || '').slice(0,100),
            testid: el.getAttribute('data-testid') || ''
        })""")
        for key in ("aria", "title", "text", "testid"):
            value = _normalise(details.get(key, ""))
            if value:
                return value
        return ""
    except Exception:
        return ""


def _near_editor(control_box: dict, editor_box: dict) -> bool:
    cx = float(control_box["x"]) + float(control_box["width"]) / 2
    cy = float(control_box["y"]) + float(control_box["height"]) / 2
    left, right = float(editor_box["x"]), float(editor_box["x"]) + float(editor_box["width"])
    center_y = float(editor_box["y"]) + float(editor_box["height"]) / 2
    return left - 360 <= cx <= right + 160 and abs(cy - center_y) <= 230


def _find_send_button(page, editor_box: dict | None = None):
    """Use the same accessible-role lookup as the proven legacy Meta provider.

    The old provider selected page.get_by_role("button", name="Send") and then
    required exactly one visible, enabled match. Avoid scanning every DOM
    button and applying guessed geometry; that caused SEND_CONTROL_NOT_FOUND
    even though the composer and prompt had already been verified.
    """
    try:
        # Playwright does substring matching for string names unless exact=True.
        # This also matches Meta UI variants whose accessible name is "Send message".
        matches = page.get_by_role("button", name="Send").all()
    except Exception:
        raise GateError("METAAI_CREATE_SEND_CONTROLS_UNOBSERVABLE") from None

    visible = []
    for item in matches:
        try:
            if not item.is_visible() or not item.is_enabled():
                continue
            label = _control_label(item)
            if DANGEROUS_LABEL.search(label) or NON_SEND_CONTROL.search(label):
                continue
            visible.append(item)
        except Exception:
            continue

    print(
        "TOOL_SYSTEM_METAAI_CREATE_SEND_MATCHES="
        + str(len(matches))
        + ";VISIBLE_ENABLED="
        + str(len(visible)),
        flush=True,
    )
    if not visible:
        # Localized UI fallback; do not relax into arbitrary buttons or coordinates.
        localized = []
        for accessible_name in ("Gửi", "Gửi tin nhắn"):
            try:
                controls = page.get_by_role("button", name=accessible_name).all()
            except Exception:
                continue
            for item in controls:
                try:
                    if not item.is_visible() or not item.is_enabled():
                        continue
                    label = _control_label(item)
                    if DANGEROUS_LABEL.search(label) or NON_SEND_CONTROL.search(label):
                        continue
                    if item not in localized:
                        localized.append(item)
                except Exception:
                    continue
        visible = localized
        print("TOOL_SYSTEM_METAAI_CREATE_SEND_LOCALIZED_MATCHES=" + str(len(visible)),
              flush=True)

    if not visible:
        raise GateError("METAAI_CREATE_SEND_CONTROL_NOT_FOUND")
    if len(visible) != 1:
        raise GateError("METAAI_CREATE_SEND_CONTROL_AMBIGUOUS")
    print("TOOL_SYSTEM_METAAI_CREATE_SEND=ONE_VISIBLE_ROLE_MATCH_VERIFIED", flush=True)
    return visible[0]


def _generation_prompt(job: Job) -> str:
    if not getattr(job, "script", None) or not getattr(job, "script_sha", None):
        raise GateError("METAAI_SCRIPT_EVIDENCE_MISSING")
    if sha256(job.script.encode("utf-8")).hexdigest() != job.script_sha:
        raise GateError("METAAI_SCRIPT_SHA_MISMATCH")
    return _meta_prompt_text(
        "Generate ONLY one actual playable AI VIDEO with Meta AI's video-generation capability "
        "on the Meta AI create page, not a still image or a text-only description. Use portrait "
        "9:16 framing and coherent motion. Follow the complete Vietnamese script in exact "
        "scene order, actions, character identities, dialogue, sound cues, and ending. "
        "Keep recurring characters visually consistent. Do not add people, props, captions, "
        "logos, watermarks, or events absent from the script. Do not publish, share, or post "
        "the result. Internal job ID/SHA are bookkeeping only; do not render them in the video. "
        "MONEY AI JOB REFERENCE: " + job.id +
        " SCRIPT SHA256: " + job.script_sha +
        " SCRIPT (Vietnamese): " + job.script +
        " All character actions and every event in the video must match the script exactly, "
        "in full, and in the specified order. Do not skip, shorten, omit, merge, or alter "
        "any action or story detail."
    )


def _reference_image_for_job(job: Job) -> Path | None:
    """Attach a reference only when a real image path was explicitly supplied."""
    raw = getattr(job, "reference_path", None) or os.environ.get("TOOL_SYSTEM_META_REFERENCE_IMAGE", "")
    if not str(raw or "").strip():
        return None
    path = Path(str(raw)).expanduser()
    if not path.is_file():
        raise GateError("METAAI_REFERENCE_IMAGE_NOT_FOUND")
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise GateError("METAAI_REFERENCE_IMAGE_TYPE_NOT_SUPPORTED")
    return path.resolve()


def _attach_reference_if_configured(page, editor, job: Job) -> bool:
    reference = _reference_image_for_job(job)
    if reference is None:
        return False
    inputs = page.locator("input[type=file]")
    if inputs.count() != 1:
        raise GateError("METAAI_REFERENCE_FILE_INPUT_NOT_UNIQUE")
    inputs.first.set_input_files(str(reference), timeout=12000)
    page.wait_for_timeout(1800)
    try:
        attached = editor.locator("span.inline-image-node").count()
    except Exception:
        attached = 0
    if attached < 1:
        raise GateError("METAAI_REFERENCE_UPLOAD_NOT_VERIFIED")
    print("TOOL_SYSTEM_METAAI_REFERENCE=ATTACHED_AND_VERIFIED", flush=True)
    return True


def _replace_composer(page, editor, text: str) -> None:
    """Fill and verify the complete text before enabling a single Send click."""
    try:
        editor.fill(text, timeout=15000)
    except Exception:
        editor.click(timeout=8000)
        page.keyboard.press("Control+A")
        page.keyboard.insert_text(text)
    page.wait_for_timeout(400)
    if _read_editor(editor) != text:
        raise GateError("METAAI_CREATE_PROMPT_FILL_NOT_VERIFIED")
    print("TOOL_SYSTEM_METAAI_PROMPT=FULL_TEXT_VERIFIED", flush=True)


def _thread_path(url: str) -> str | None:
    try:
        current = urlparse(url)
        if (
            current.scheme == "https"
            and (current.hostname or "").lower() in META_HOSTS
            and PROMPT_PATH.fullmatch(current.path or "")
        ):
            return (current.path or "").rstrip("/")
    except Exception:
        pass
    return None


def _composer_value(page) -> str:
    try:
        rows = _visible_composer_rows(page)
        if len(rows) == 1:
            return rows[0]["text"]
        if len(rows) > 1:
            matching = [row["text"] for row in rows
                        if "MONEY AI JOB REFERENCE:" in row["text"]]
            if len(matching) == 1:
                return matching[0]
    except Exception:
        pass
    return ""


def _prior_prompt_state(page, job: Job) -> str:
    if not getattr(job, "id", None) or not getattr(job, "script_sha", None):
        return "NO_MATCH"
    marker = "MONEY AI JOB REFERENCE: " + job.id
    sha_marker = "SCRIPT SHA256: " + job.script_sha
    body = _body_text(page)
    composer = _composer_value(page)
    if marker in composer and sha_marker in composer:
        return "UNSUBMITTED_COMPOSER"
    history = body.replace(composer, "", 1) if composer else body
    if marker in history and sha_marker in history:
        return "SUBMITTED_HISTORY" if _thread_path(page.url) else "SUBMITTED_NO_ROUTE"
    return "NO_MATCH"


def _find_meta_tab_for_job(context, job: Job):
    try:
        pages = list(context.pages)
    except Exception:
        pages = []
    matches = []
    for page in pages:
        try:
            if page.is_closed() or _host(page.url) not in META_HOSTS:
                continue
            state = _prior_prompt_state(page, job)
            if state in {"UNSUBMITTED_COMPOSER", "SUBMITTED_HISTORY", "SUBMITTED_NO_ROUTE"}:
                matches.append((0 if state != "UNSUBMITTED_COMPOSER" else 1, page, state))
        except Exception:
            continue
    if not matches:
        return None, "NO_MATCH"
    matches.sort(key=lambda row: row[0])
    best = [row for row in matches if row[0] == matches[0][0]]
    if len(best) != 1:
        raise GateError("METAAI_PRIOR_JOB_MATCH_AMBIGUOUS")
    _, page, state = best[0]
    page.bring_to_front()
    print("TOOL_SYSTEM_METAAI_PRIOR_TAB=MATCHED_JOB_REFERENCE_AND_SHA", flush=True)
    print("TOOL_SYSTEM_METAAI_PRIOR_TAB_STATE=" + state, flush=True)
    return page, state


def _visible_video(page):
    try:
        videos = page.locator("video:visible")
        count = min(videos.count(), 20)
    except Exception:
        raise GateError("METAAI_VIDEO_DOM_UNOBSERVABLE") from None
    rows = []
    for index in range(count):
        item = videos.nth(index)
        try:
            if not item.is_visible():
                continue
            box = item.bounding_box()
            if box and float(box["width"]) >= 80 and float(box["height"]) >= 60:
                rows.append((item, box))
        except Exception:
            continue
    if len(rows) > 1:
        raise GateError("METAAI_VIDEO_CANDIDATE_NOT_UNIQUE")
    return rows[0] if rows else None


def _find_download_button(page, video_box: dict):
    choices = []
    try:
        buttons = page.get_by_role("button")
        count = min(buttons.count(), 220)
    except Exception:
        raise GateError("METAAI_DOWNLOAD_CONTROLS_UNOBSERVABLE") from None
    for index in range(count):
        item = buttons.nth(index)
        try:
            if not item.is_visible() or not item.is_enabled():
                continue
            box = item.bounding_box()
            if not box or float(box["width"]) < 10 or float(box["height"]) < 10:
                continue
            label = _control_label(item)
            if DANGEROUS_LABEL.search(label):
                continue
            try:
                accessible = _normalise(item.get_attribute("aria-label") or "")
                title = _normalise(item.get_attribute("title") or "")
            except Exception:
                accessible, title = "", ""
            if not any(DOWNLOAD_LABEL.fullmatch(t.strip()) for t in (label, accessible, title)):
                continue
            dx = abs((float(box["x"]) + float(box["width"]) / 2)
                     - (float(video_box["x"]) + float(video_box["width"]) / 2))
            dy = abs((float(box["y"]) + float(box["height"]) / 2)
                     - (float(video_box["y"]) + float(video_box["height"]) / 2))
            if dx > max(700, float(video_box["width"]) * 1.4) or dy > 850:
                continue
            choices.append((dx + dy, item, label or accessible or title))
        except Exception:
            continue
    if not choices:
        return None
    choices.sort(key=lambda row: row[0])
    if len(choices) > 1 and abs(choices[0][0] - choices[1][0]) < 1:
        raise GateError("METAAI_DOWNLOAD_CONTROL_AMBIGUOUS")
    return choices[0][1], choices[0][2]


def _download_video(page, video_box: dict, job: Job) -> Path:
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    destination = DOWNLOADS / ("meta_ai_" + job.id + ".mp4")
    if destination.is_file() and destination.stat().st_size > 0:
        print("TOOL_SYSTEM_METAAI_DOWNLOAD=EXISTING_JOB_FILE_REUSED", flush=True)
        return destination
    choice = _find_download_button(page, video_box)
    if choice is None:
        raise GateError("METAAI_DOWNLOAD_CONTROL_NOT_FOUND")
    control, label = choice
    print("TOOL_SYSTEM_METAAI_DOWNLOAD_CONTROL=" +
          re.sub(r"[^A-Z0-9_]", "_", label.upper())[:60], flush=True)
    try:
        with page.expect_download(timeout=60000) as event:
            control.click(timeout=9000)
        download = event.value
        download.save_as(str(destination))
        if download.failure():
            raise GateError("METAAI_DOWNLOAD_FAILED")
    except PlaywrightTimeoutError:
        raise GateError("METAAI_DOWNLOAD_EVENT_NOT_VERIFIED") from None
    except GateError:
        raise
    except Exception as exc:
        raise GateError("METAAI_DOWNLOAD_FAILED_" + type(exc).__name__) from None
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise GateError("METAAI_DOWNLOADED_MP4_EMPTY")
    print("TOOL_SYSTEM_METAAI=DOWNLOAD_EVENT_AND_FILE_VERIFIED", flush=True)
    print("TOOL_SYSTEM_METAAI_DOWNLOAD_SIZE=" + str(destination.stat().st_size), flush=True)
    return destination


def _diagnose_failure_text(body: str) -> str | None:
    for code, pattern in FAILURE_TEXT.items():
        if pattern.search(body[-8000:]):
            return code
    return None


def _artifact_from_current_thread(page, job: Job) -> MetaWebArtifact | None:
    body = _body_text(page)
    if ("MONEY AI JOB REFERENCE: " + job.id) not in body:
        return None
    if ("SCRIPT SHA256: " + job.script_sha) not in body:
        return None
    visible = _visible_video(page)
    if visible is None:
        return None
    _video, box = visible
    if _find_download_button(page, box) is None:
        return None
    path = _download_video(page, box, job)
    return MetaWebArtifact(path, page.url, job.script_sha, job.id,
                           "META_CREATE_THREAD_JOB_SHA_AND_VIDEO_DOWNLOAD")


def _wait_for_prompt_thread(page, seconds: float = 35.0) -> str:
    deadline = time.monotonic() + max(0.1, float(seconds))
    while time.monotonic() < deadline:
        if _host(page.url) not in META_HOSTS:
            raise GateError("METAAI_LEFT_APPROVED_DOMAIN")
        path = _thread_path(page.url)
        if path:
            print("TOOL_SYSTEM_METAAI_SUBMIT_RECEIPT=THREAD_UUID_OBSERVED", flush=True)
            return path
        page.wait_for_timeout(900)
    raise GateError("METAAI_SUBMITTED_THREAD_ID_UNKNOWN_NO_RETRY")


def inspect_prior_generation(job: Job, *, browser_factory=None) -> MetaWebReconciliation:
    """Read-only reconciliation against job ID/SHA; never clicks Send."""
    factory = browser_factory or Chrome
    with factory(profile_dir=PROFILE, downloads_dir=DOWNLOADS,
                 headless=False, cdp_url="http://127.0.0.1:9222") as browser:
        context = getattr(browser, "context", None)
        if context is None:
            raise GateError("METAAI_CHROME_CONTEXT_MISSING")
        page, state = _find_meta_tab_for_job(context, job)
        if page is None:
            page = _select_or_create_meta_create_tab(context)
            if not _page_is_logged_in(page):
                raise GateError("METAAI_OWNER_LOGIN_REQUIRED")
            _wait_for_create_surface(page)
            print("TOOL_SYSTEM_METAAI_RECONCILIATION_STATE=NO_MATCH_ON_META_CREATE", flush=True)
            return MetaWebReconciliation(state="NO_MATCHING_PRIOR_PROMPT_ON_META_CREATE")
        if not _page_is_logged_in(page):
            raise GateError("METAAI_OWNER_LOGIN_REQUIRED")
        if state == "UNSUBMITTED_COMPOSER":
            if (urlparse(page.url).path or "").rstrip("/") != "/create":
                raise GateError("METAAI_PRIOR_COMPOSER_NOT_ON_CREATE_SURFACE")
            text = _composer_value(page)
            if ("MONEY AI JOB REFERENCE: " + job.id not in text
                    or "SCRIPT SHA256: " + job.script_sha not in text):
                raise GateError("METAAI_PRIOR_COMPOSER_JOB_MARKER_MISMATCH")
            return MetaWebReconciliation(state="UNSUBMITTED_COMPOSER")
        if state in {"SUBMITTED_HISTORY", "SUBMITTED_NO_ROUTE"}:
            if not _thread_path(page.url):
                return MetaWebReconciliation(state="SUBMITTED_NO_ASSOCIATED_VIDEO")
            artifact = _artifact_from_current_thread(page, job)
            if artifact is None:
                return MetaWebReconciliation(state="SUBMITTED_NO_ASSOCIATED_VIDEO")
            return MetaWebReconciliation(state="VIDEO_RECOVERED", artifact=artifact)
        if (urlparse(page.url).path or "").rstrip("/") != "/create":
            page.goto(META_CREATE_URL, wait_until="domcontentloaded", timeout=45000)
        _wait_for_create_surface(page)
        return MetaWebReconciliation(state="NO_MATCHING_PRIOR_PROMPT_ON_META_CREATE")


def create_video_from_script(job: Job, *, browser_factory=None) -> MetaWebArtifact:
    """Fill Meta AI /create, click Send once, verify receipt, and download the video."""
    if job.stage != "SCRIPT_READY":
        raise GateError("METAAI_GENERATION_REQUIRES_SCRIPT_READY")
    if not job.script or not job.script_sha:
        raise GateError("METAAI_SCRIPT_EVIDENCE_MISSING")
    if sha256(job.script.encode("utf-8")).hexdigest() != job.script_sha:
        raise GateError("METAAI_SCRIPT_SHA_MISMATCH")
    prompt = _generation_prompt(job)
    factory = browser_factory or Chrome
    with factory(profile_dir=PROFILE, downloads_dir=DOWNLOADS,
                 headless=False, cdp_url="http://127.0.0.1:9222") as browser:
        context = getattr(browser, "context", None)
        if context is None:
            raise GateError("METAAI_CHROME_CONTEXT_MISSING")
        page, state = _find_meta_tab_for_job(context, job)
        if page is None:
            page = _select_or_create_meta_create_tab(context)
        if not _page_is_logged_in(page):
            raise GateError("METAAI_OWNER_LOGIN_REQUIRED")

        if state in {"SUBMITTED_HISTORY", "SUBMITTED_NO_ROUTE"}:
            if not _thread_path(page.url):
                raise GateError("METAAI_PRIOR_REQUEST_OUTCOME_UNKNOWN_NO_RESUBMIT")
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                failure = _diagnose_failure_text(_body_text(page))
                if failure:
                    raise GateError(failure)
                artifact = _artifact_from_current_thread(page, job)
                if artifact is not None:
                    print("TOOL_SYSTEM_METAAI=PRIOR_JOB_VIDEO_RECOVERED", flush=True)
                    return artifact
                page.wait_for_timeout(4000)
            raise GateError("METAAI_PRIOR_REQUEST_OUTCOME_UNKNOWN_NO_RESUBMIT")

        if state == "UNSUBMITTED_COMPOSER":
            if (urlparse(page.url).path or "").rstrip("/") != "/create":
                raise GateError("METAAI_PRIOR_COMPOSER_NOT_ON_CREATE_SURFACE")
            editor, _box = _find_composer(page)
            current = _composer_value(page)
            if ("MONEY AI JOB REFERENCE: " + job.id not in current
                    or "SCRIPT SHA256: " + job.script_sha not in current):
                raise GateError("METAAI_PRIOR_COMPOSER_JOB_MARKER_MISMATCH")
            _replace_composer(page, editor, prompt)
        else:
            if (urlparse(page.url).path or "").rstrip("/") != "/create":
                page.goto(META_CREATE_URL, wait_until="domcontentloaded", timeout=45000)
            _wait_for_create_surface(page)
            editor, _box = _find_composer(page)
            if _composer_value(page):
                raise GateError("METAAI_CREATE_COMPOSER_HAS_UNRELATED_TEXT")
            try:
                image_nodes = editor.locator("span.inline-image-node").count()
            except Exception:
                image_nodes = 0
            if image_nodes and _reference_image_for_job(job) is None:
                raise GateError("METAAI_CREATE_COMPOSER_HAS_UNBOUND_REFERENCE")
            _attach_reference_if_configured(page, editor, job)
            _replace_composer(page, editor, prompt)

        if not _create_surface_verified(page):
            raise GateError("METAAI_CREATE_SURFACE_LOST_BEFORE_SUBMIT")
        editor, box = _find_composer(page)
        if _composer_value(page) != prompt:
            raise GateError("METAAI_CREATE_PROMPT_FILL_NOT_VERIFIED")
        send = _find_send_button(page, box)
        # Exactly one live Send click; unknown outcomes are reconciled, never retried.
        send.click(timeout=9000)
        print("TOOL_SYSTEM_METAAI_SUBMIT=ONE_VERIFIED_SEND_CLICK", flush=True)
        _wait_for_prompt_thread(page)

        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            if _host(page.url) not in META_HOSTS:
                raise GateError("METAAI_LEFT_APPROVED_DOMAIN")
            if not _thread_path(page.url):
                raise GateError("METAAI_THREAD_ROUTE_LOST_AFTER_SUBMIT")
            body = _body_text(page)
            failure = _diagnose_failure_text(body)
            if failure:
                raise GateError(failure)
            if ("MONEY AI JOB REFERENCE: " + job.id in body
                    and "SCRIPT SHA256: " + job.script_sha in body):
                artifact = _artifact_from_current_thread(page, job)
                if artifact is not None:
                    print("TOOL_SYSTEM_METAAI=NEW_VIDEO_DOWNLOADED", flush=True)
                    return artifact
            page.wait_for_timeout(4000)
        raise GateError("METAAI_VIDEO_RESULT_NOT_VERIFIED_WITHIN_TIMEOUT")
