"""Single-instance Chrome adapter using deterministic Python-only browser rules.

This module reuses the existing owner-visible Chrome through CDP 9222. The
operator observes live DOM controls, applies bounded allowlisted rules, checks
each target, and verifies download evidence. It never loads or calls a model.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
import os
import re
import json
import shutil
from typing import Any

from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout

from .core import GateError


ALLOWED = {
    "gemini.google.com", "labs.google", "labs.google.com",
    "flow.google", "flow.google.com", "meta.ai", "www.meta.ai",
    "facebook.com", "www.facebook.com", "business.facebook.com",
}
_DANGEROUS_CONTROL = re.compile(
    r"publish|\bpost\b|share|delete|remove|sign.?in|log.?in|logout|log out|"
    r"accept|agree|consent|payment|checkout|subscribe|purchase|account|profile|cancel|close|"
    r"đăng bài|đăng ngay|chia sẻ|xóa|xoá|đăng nhập|đồng ý|chấp nhận",
    re.I,
)
_GENERATE_VIDEO = re.compile(r"generate video|create video|tạo video|video tạo", re.I)
_GENERATE_ACTION = re.compile(r"generate|create|tạo|make video|video", re.I)
_DOWNLOAD_ACTION = re.compile(r"download(?: video| mp4| file)?|tải xuống|tải video", re.I)


@dataclass(frozen=True)
class BrowserTaskResult:
    page_url: str
    body_text: str
    download_path: str | None
    steps: int


class Chrome:
    def __init__(self, profile_dir: str | Path, downloads_dir: str | Path,
                 headless: bool = False, chromium_path: str | None = None,
                 cdp_url: str | None = None):
        self.profile_dir = Path(profile_dir)
        self.downloads_dir = Path(downloads_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.chromium_path = (
            chromium_path or os.getenv("CHROMIUM_PATH") or shutil.which("chromium")
        )
        self.cdp_url = cdp_url or os.getenv("TOOL_SYSTEM_CDP_URL")
        self._shared_browser = None
        self._playwright = None
        self.context = None
        self.page = None

    def __enter__(self):
        if not self.headless and os.name == "posix" and not os.environ.get("DISPLAY"):
            raise RuntimeError(
                "VISIBLE_CHROME_REQUIRES_DISPLAY: start the existing Kaggle CPU display first"
            )
        if Path("/kaggle").exists() and not self.cdp_url:
            raise RuntimeError(
                "KAGGLE_ONE_CHROME_REQUIRES_CDP: use the shared Chrome on port 9222"
            )
        self._playwright = sync_playwright().start()
        try:
            if self.cdp_url:
                if self.headless:
                    raise RuntimeError("SHARED_CHROME_MUST_BE_VISIBLE")
                if self.cdp_url != "http://127.0.0.1:9222":
                    raise RuntimeError("CDP_MUST_USE_LOCAL_KAGGLE_CHROME")
                self._shared_browser = self._playwright.chromium.connect_over_cdp(self.cdp_url)
                if len(self._shared_browser.contexts) != 1:
                    raise RuntimeError("ONE_CHROME_ONE_PROFILE_REQUIRED")
                self.context = self._shared_browser.contexts[0]
                self.context.set_default_timeout(15000)
            else:
                self.context = self._playwright.chromium.launch_persistent_context(
                    str(self.profile_dir), headless=self.headless,
                    executable_path=self.chromium_path, accept_downloads=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        except Exception:
            self._playwright.stop()
            raise
        return self

    def __exit__(self, *args):
        # Never close the shared owner's Chrome, context or pages.
        if self.context and not self._shared_browser:
            self.context.close()
        if self._playwright:
            self._playwright.stop()

    @staticmethod
    def _allow(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED:
            raise GateError("CHROME_NAVIGATION_DOMAIN_NOT_APPROVED")

    def _snapshot(self) -> dict:
        if not self.page:
            raise RuntimeError("CHROME_CONTEXT_NOT_STARTED")
        from .adaptive_browser import OBSERVE_JS
        observed = self.page.evaluate(OBSERVE_JS)
        if not isinstance(observed, dict):
            raise GateError("CHROME_LIVE_DOM_OBSERVATION_INVALID")
        return observed

    def _observe(self) -> str:
        if not self.page:
            raise RuntimeError("CHROME_CONTEXT_NOT_STARTED")
        body = self.page.locator("body").inner_text(timeout=15000)[:6500]
        observed = self._snapshot()
        clean = {
            "origin": observed.get("url"),
            "editor": observed.get("editor"),
            "control_ids": observed.get("controls", [])[:90],
        }
        return ("URL: " + self.page.url + "\nVISIBLE TEXT:\n" + body +
                "\nLIVE_CONTROL_IDS:\n" + json.dumps(clean, ensure_ascii=False)[:12500])

    def _login_gate(self) -> None:
        if not self.page:
            raise RuntimeError("CHROME_CONTEXT_NOT_STARTED")
        try:
            if self.page.locator('input[type="password"]').count():
                raise GateError("OWNER_LOGIN_REQUIRED")
            host = (urlparse(self.page.url).hostname or "").lower()
            if host in {"accounts.google.com", "login.facebook.com"}:
                raise GateError("OWNER_LOGIN_REQUIRED")
        except GateError:
            raise
        except Exception as exc:
            raise GateError("CHROME_LOGIN_STATE_UNVERIFIED") from None

    def _classify_page_state(self) -> str:
        self._login_gate()
        try:
            if self.page.locator('[aria-modal="true"],[role="dialog"],dialog,[role="alertdialog"]').count():
                return "CONSENT_REQUIRED"
        except Exception:
            return "UI_STATE_UNVERIFIED"
        return "COMPOSER_NOT_RENDERED"

    def _find_prompt_input(self):
        """Choose one visible editable prompt by accessibility hints; abstain if ambiguous."""
        if not self.page:
            raise RuntimeError("CHROME_CONTEXT_NOT_STARTED")
        selectors = (
            'textarea:not([disabled]):not([readonly])',
            '[contenteditable="true"]',
            '[role="textbox"]',
            'input[type="text"]:not([disabled]):not([readonly])',
        )
        rows = []
        for selector in selectors:
            locators = self.page.locator(selector)
            try:
                count = min(locators.count(), 50)
            except Exception:
                continue
            for index in range(count):
                candidate = locators.nth(index)
                try:
                    if not (candidate.is_visible() and candidate.is_enabled()
                            and candidate.is_editable()):
                        continue
                    box = candidate.bounding_box()
                    if not box or box["width"] < 100 or box["height"] < 14:
                        continue
                    details = candidate.evaluate("""el => ({
                        tag: el.tagName.toLowerCase(),
                        role: el.getAttribute('role') || '',
                        aria: el.getAttribute('aria-label') || '',
                        placeholder: el.getAttribute('placeholder') || '',
                        name: el.getAttribute('name') || '',
                        text: (el.innerText || '').slice(0,120),
                        width: el.getBoundingClientRect().width,
                        height: el.getBoundingClientRect().height
                    })""")
                    label = " ".join(str(details.get(k, "")) for k in (
                        "aria", "placeholder", "name", "text", "role"
                    )).casefold()
                    score = 0
                    if re.search(r"prompt|describe|ask|message|script|create|video|chat|type", label):
                        score += 10
                    if details.get("tag") == "textarea":
                        score += 2
                    area = float(details.get("width", 0)) * float(details.get("height", 0))
                    signature = (
                        round(float(box["x"])), round(float(box["y"])),
                        round(float(box["width"])), round(float(box["height"])),
                    )
                    if not any(row["signature"] == signature for row in rows):
                        rows.append({"locator": candidate, "score": score, "area": area,
                                     "signature": signature, "label": label})
                except Exception:
                    continue
        if not rows:
            raise GateError("CHROME_PROMPT_INPUT_NOT_FOUND")
        rows.sort(key=lambda row: (row["score"], row["area"]), reverse=True)
        if len(rows) > 1 and rows[0]["score"] == rows[1]["score"]:
            difference = abs(rows[0]["area"] - rows[1]["area"])
            if difference < max(rows[0]["area"], rows[1]["area"]) * 0.25:
                raise GateError("CHROME_PROMPT_INPUT_AMBIGUOUS")
        return rows[0]["locator"]

    @staticmethod
    def _control_label(control: dict) -> str:
        return " ".join(str(control.get(k, "")) for k in ("hint", "label", "aria", "role")).strip()

    def _rank_control(self, controls: list[dict], *, mode: str, host: str,
                      excluded: set[tuple] | None = None) -> dict:
        """Select one observed control by task-specific Python rules only."""
        excluded = excluded or set()
        candidates = []
        for control in controls:
            if control.get("disabled"):
                continue
            label = self._control_label(control)
            normalized = " ".join(label.split()).casefold()
            signature = (
                normalized, round(float(control.get("x", 0))),
                round(float(control.get("y", 0))),
            )
            if signature in excluded or _DANGEROUS_CONTROL.search(label):
                continue
            if mode == "generate":
                if _GENERATE_VIDEO.search(label):
                    score = 12
                elif _GENERATE_ACTION.search(label):
                    score = 8
                elif host in {"meta.ai", "www.meta.ai"} and control.get("near") and re.fullmatch(
                        r"(?:send|submit|gửi|enter)", normalized):
                    score = 5
                else:
                    continue
            elif mode == "download":
                if not _DOWNLOAD_ACTION.search(label):
                    continue
                score = 12 if _GENERATE_VIDEO.search(label) else (
                    10 if re.search(r"download video|download mp4|tải video", normalized) else 6
                )
                if "app" in normalized or "extension" in normalized:
                    continue
            else:
                raise ValueError("Unsupported deterministic Chrome control mode")
            candidates.append((score, control, signature))
        if not candidates:
            raise GateError("CHROME_" + mode.upper() + "_CONTROL_NOT_FOUND")
        best_score = max(item[0] for item in candidates)
        best = [item for item in candidates if item[0] == best_score]
        if len(best) != 1:
            raise GateError("CHROME_" + mode.upper() + "_CONTROL_AMBIGUOUS")
        return best[0][1]

    def _click_observed_control(self, control: dict, *, allow_menu: bool = False) -> None:
        if not self.page:
            raise RuntimeError("CHROME_CONTEXT_NOT_STARTED")
        from .adaptive_browser import SAFE_CLICK_JS
        label = self._control_label(control)
        result = self.page.evaluate(SAFE_CLICK_JS, {
            "x": float(control["x"]), "y": float(control["y"]),
            "allowMenu": allow_menu, "expected": label[:70],
        })
        if not isinstance(result, dict) or not result.get("ok"):
            raise GateError("CHROME_TARGET_HIT_TEST_REJECTED")
        self.page.mouse.click(float(control["x"]), float(control["y"]))

    def run(self, start_url: str, goal: str, *, upload_path: str | None = None,
            allow_facebook_publish: bool = False, max_steps: int = 120,
            lessons: list[dict] | None = None) -> BrowserTaskResult:
        """Create/download one video via grounded Python UI rules; never auto-publish."""
        del allow_facebook_publish, lessons
        if not self.page:
            raise RuntimeError("Use Chrome as context manager")
        if not 1 <= max_steps <= 180:
            raise ValueError("max_steps must be in range 1..180")
        self._allow(start_url)
        start_host = (urlparse(start_url).hostname or "").lower()
        if start_host not in _ALLOWED_VIDEO_HOSTS:
            raise GateError("CHROME_VIDEO_PROVIDER_NOT_APPROVED")
        if self.page.url != start_url:
            self.page.goto(start_url, wait_until="domcontentloaded", timeout=45000)
        self._login_gate()

        if upload_path is not None:
            source = Path(upload_path)
            if not source.is_file() or source.suffix.lower() != ".mp4":
                raise GateError("CHROME_UPLOAD_SOURCE_NOT_VERIFIED")
            from .adaptive_browser import AdaptiveUploader
            AdaptiveUploader().upload(self.page, provider=start_host, file=source)

        # Reuse the actual visible provider composer. Do not use a guessed selector,
        # type into history or click a prior result card.
        try:
            editor = self._find_prompt_input()
        except GateError as first:
            if str(first) not in {
                "CHROME_PROMPT_INPUT_NOT_FOUND", "CHROME_PROMPT_INPUT_AMBIGUOUS"
            }:
                raise
            from .tool_recovery import recover_editor
            editor = recover_editor(
                self.page, probe=self._find_prompt_input,
                classify=self._classify_page_state, max_actions=3,
                provider=start_host, error_prefix="CHROME",
            )
        self._login_gate()
        try:
            editor.fill(goal, timeout=12000)
            typed = editor.evaluate("(el) => ('value' in el ? el.value : el.innerText || '')")
        except Exception as exc:
            raise GateError("CHROME_PROMPT_FILL_FAILED_" + type(exc).__name__) from None
        if not isinstance(typed, str) or len(typed.strip()) < max(40, int(len(goal.strip()) * 0.85)):
            raise GateError("CHROME_PROMPT_FILL_NOT_VERIFIED")

        baseline_body = self.page.locator("body").inner_text(timeout=12000)[:24000]
        baseline_video = self.page.locator("video").count()
        baseline_controls = self._snapshot().get("controls", [])
        baseline_signatures = {
            (" ".join(self._control_label(c).split()).casefold(),
             round(float(c.get("x", 0))), round(float(c.get("y", 0))))
            for c in baseline_controls
        }
        controls = self._snapshot().get("controls", [])
        submit = self._rank_control(controls, mode="generate", host=start_host)
        self._login_gate()
        self._click_observed_control(submit)
        print("MONEY_TOOL_VIDEO_SUBMIT=OBSERVED_CONTROL_CLICKED", flush=True)

        download_path = None
        last_body = baseline_body
        for step in range(1, max_steps + 1):
            self.page.wait_for_timeout(5000)
            self._allow(self.page.url)
            if (urlparse(self.page.url).hostname or "").lower() not in _ALLOWED_VIDEO_HOSTS:
                raise GateError("CHROME_VIDEO_PROVIDER_NAVIGATED_OUTSIDE_APPROVED_DOMAIN")
            self._login_gate()
            body = self.page.locator("body").inner_text(timeout=12000)[:24000]
            controls = self._snapshot().get("controls", [])
            video_count = self.page.locator("video").count()
            result_changed = (body != baseline_body and len(body) >= 80) or video_count > baseline_video
            if not result_changed:
                last_body = body
                continue
            fresh_download_controls = []
            for control in controls:
                label = self._control_label(control)
                sig = (" ".join(label.split()).casefold(),
                       round(float(control.get("x", 0))),
                       round(float(control.get("y", 0))))
                if sig not in baseline_signatures and _DOWNLOAD_ACTION.search(label):
                    fresh_download_controls.append(control)
            candidates = fresh_download_controls
            if not candidates:
                try:
                    candidate = self._rank_control(controls, mode="download", host=start_host)
                    label = self._control_label(candidate)
                    sig = (" ".join(label.split()).casefold(),
                           round(float(candidate.get("x", 0))),
                           round(float(candidate.get("y", 0))))
                    # A control already present before submission is not evidence
                    # of a newly generated artifact; wait for a current result.
                    if sig not in baseline_signatures:
                        candidates = [candidate]
                except GateError:
                    candidates = []
            if len(candidates) > 1:
                raise GateError("CHROME_DOWNLOAD_CONTROL_AMBIGUOUS")
            if len(candidates) == 1:
                candidate = candidates[0]
                self._login_gate()
                try:
                    with self.page.expect_download(timeout=30000) as event:
                        self._click_observed_control(candidate)
                    item = event.value
                    self.downloads_dir.mkdir(parents=True, exist_ok=True)
                    safe_name = Path(item.suggested_filename).name
                    destination = self.downloads_dir / safe_name
                    item.save_as(str(destination))
                except BrowserTimeout:
                    raise GateError("CHROME_DOWNLOAD_EVENT_NOT_VERIFIED") from None
                except GateError:
                    raise
                except Exception as exc:
                    raise GateError("CHROME_DOWNLOAD_FAILED_" + type(exc).__name__) from None
                if not destination.is_file() or destination.stat().st_size <= 0:
                    raise GateError("CHROME_DOWNLOADED_FILE_EMPTY")
                download_path = str(destination)
                print("MONEY_TOOL_VIDEO_DOWNLOAD=FILE_RECEIVED", flush=True)
                return BrowserTaskResult(
                    self.page.url, self._observe(), download_path, step
                )
            last_body = body
        raise GateError("CHROME_VIDEO_RESULT_OR_DOWNLOAD_NOT_VERIFIED")
