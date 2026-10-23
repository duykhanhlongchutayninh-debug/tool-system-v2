"""Python-only adapter for an already-open async Kaggle Chrome page.

Never creates/closes a browser or calls a reasoning model. Actions are chosen
from fresh DOM control evidence and fixed task-specific rules; ambiguous states
remain blocked.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import re
from typing import Any

from .chrome import Chrome
from .core import GateError

_DANGEROUS = re.compile(
    r"sign.?in|log.?in|consent|accept|agree|payment|checkout|subscribe|"
    r"delete|remove|publish|post now|share|đăng bài|đăng ngay|đăng nhập|"
    r"đồng ý|chấp nhận|xóa|xoá|chia sẻ",
    re.I,
)
_GENERATE = re.compile(r"(?:generate|create|tạo|make).{0,100}video|video.{0,100}(?:generate|create|tạo)", re.I)
_DOWNLOAD = re.compile(r"download(?: video| mp4| file)?|tải xuống|tải video", re.I)
_INSPECT_ONLY = re.compile(r"^(?:inspect|observe|describe|check status|describe visible controls)\b", re.I)
_ACTIONS = {
    "settings": re.compile(r"settings|cài đặt", re.I),
    "new_chat": re.compile(r"new chat|new conversation|trò chuyện mới|cuộc trò chuyện mới", re.I),
    "retry": re.compile(r"retry|try again|thử lại", re.I),
    "add_files": re.compile(r"add files|attach file|đính kèm|thêm tệp", re.I),
    "upload_files": re.compile(r"upload files|tải tệp lên", re.I),
    "generate": _GENERATE,
    "download": _DOWNLOAD,
}


class NotebookChrome:
    """Reuse one existing user-visible async Playwright page; never launch Chrome."""

    def __init__(self, *, page: Any,
                 downloads_dir: str | Path = "/kaggle/working/tool-system-v2-runtime/downloads"):
        if page is None:
            raise RuntimeError("KAGGLE_EXISTING_PAGE_REQUIRED")
        if page.is_closed():
            raise RuntimeError("KAGGLE_EXISTING_PAGE_CLOSED")
        self.page = page
        self.downloads_dir = Path(downloads_dir)

    async def _snapshot(self) -> dict:
        if self.page.is_closed():
            raise RuntimeError("KAGGLE_EXISTING_PAGE_CLOSED")
        from .adaptive_browser import OBSERVE_JS
        try:
            result = await self.page.evaluate(OBSERVE_JS)
        except Exception as exc:
            raise RuntimeError("EXISTING_CHROME_INSPECTION_FAILED:" +
                               type(exc).__name__) from None
        if not isinstance(result, dict) or not isinstance(result.get("controls"), list):
            raise GateError("EXISTING_CHROME_DOM_OBSERVATION_INVALID")
        return result

    async def inspect(self) -> str:
        """Read-only observation of the exact Notebook page. No navigation."""
        try:
            body = await self.page.locator("body").inner_text(timeout=12000)
            snapshot = await self._snapshot()
        except Exception as exc:
            raise RuntimeError("EXISTING_CHROME_INSPECTION_FAILED:" +
                               type(exc).__name__) from None
        controls = [{
            "id": c.get("id"), "role": c.get("role"),
            "label": str(c.get("hint", ""))[:90],
            "disabled": bool(c.get("disabled")),
        } for c in snapshot.get("controls", [])[:65]]
        return (f"URL: {self.page.url}\nVISIBLE TEXT:\n{body[:9000]}"
                f"\nCONTROLS:\n{controls!r}"[:13500])

    async def _login_gate(self) -> None:
        host = (self.page.url.split("/", 3)[2] if "://" in self.page.url else "").lower()
        if host in {"accounts.google.com", "login.facebook.com"}:
            raise GateError("OWNER_LOGIN_REQUIRED")
        try:
            if await self.page.locator('input[type="password"]').count():
                raise GateError("OWNER_LOGIN_REQUIRED")
        except GateError:
            raise
        except Exception:
            raise GateError("OWNER_LOGIN_STATE_UNVERIFIED") from None

    @staticmethod
    def _label(control: dict) -> str:
        return " ".join(str(control.get(k, "")) for k in ("hint", "label", "aria", "role")).strip()

    def _select_control(self, controls: list[dict], goal: str, *, mode: str) -> dict:
        if mode == "control":
            patterns = [pattern for key, pattern in _ACTIONS.items()
                        if key in goal.casefold() or pattern.search(goal)]
            if not patterns:
                for key, pattern in _ACTIONS.items():
                    if pattern.search(goal):
                        patterns = [pattern]
                        break
            # Avoid loose matches: only explicit action phrases or known UI intents.
            if not patterns:
                raise GateError("NOTEBOOK_PYTHON_ACTION_INTENT_UNKNOWN")
        elif mode == "generate":
            patterns = [_GENERATE]
        elif mode == "download":
            patterns = [_DOWNLOAD]
        else:
            raise ValueError("Unsupported deterministic action")

        ranked = []
        for control in controls:
            if control.get("disabled"):
                continue
            label = self._label(control)
            if not label or _DANGEROUS.search(label):
                continue
            if any(pattern.search(label) for pattern in patterns):
                score = 10 if re.search(r"generate video|create video|tạo video|download video|download mp4|tải video", label, re.I) else 5
                ranked.append((score, control))
        if not ranked:
            raise GateError("NOTEBOOK_PYTHON_CONTROL_NOT_FOUND")
        best_score = max(score for score, _ in ranked)
        best = [control for score, control in ranked if score == best_score]
        if len(best) != 1:
            raise GateError("NOTEBOOK_PYTHON_CONTROL_AMBIGUOUS")
        return best[0]

    async def _click_control(self, control: dict, *, allow_menu: bool = False) -> None:
        from .adaptive_browser import SAFE_CLICK_JS
        label = self._label(control)
        try:
            proof = await self.page.evaluate(SAFE_CLICK_JS, {
                "x": float(control["x"]), "y": float(control["y"]),
                "allowMenu": allow_menu, "expected": label[:70],
            })
            if not isinstance(proof, dict) or not proof.get("ok"):
                raise GateError("NOTEBOOK_PYTHON_HIT_TEST_REJECTED")
            await self.page.mouse.click(float(control["x"]), float(control["y"]))
        except GateError:
            raise
        except Exception as exc:
            raise GateError("NOTEBOOK_PYTHON_CLICK_FAILED_" + type(exc).__name__) from None

    async def _fill_prompt(self, goal: str) -> None:
        candidates = []
        for selector in ('textarea:not([disabled]):not([readonly])',
                         '[contenteditable="true"]', '[role="textbox"]',
                         'input[type="text"]:not([disabled]):not([readonly])'):
            locators = self.page.locator(selector)
            try:
                count = min(await locators.count(), 30)
            except Exception:
                continue
            for i in range(count):
                node = locators.nth(i)
                try:
                    if not (await node.is_visible() and await node.is_enabled()
                            and await node.is_editable()):
                        continue
                    box = await node.bounding_box()
                    if not box or box["width"] < 100 or box["height"] < 14:
                        continue
                    info = await node.evaluate("""el => ({
                        tag: el.tagName.toLowerCase(),
                        label: [el.getAttribute('aria-label'),el.getAttribute('placeholder'),
                                el.getAttribute('name'),el.getAttribute('role'),
                                el.innerText||''].filter(Boolean).join(' ').slice(0,250),
                        x: Math.round(el.getBoundingClientRect().x),
                        y: Math.round(el.getBoundingClientRect().y),
                        w: Math.round(el.getBoundingClientRect().width),
                        h: Math.round(el.getBoundingClientRect().height)
                    })""")
                    label = str(info.get("label", "")).casefold()
                    score = 10 if re.search(r"prompt|describe|ask|message|script|create|video|chat|type", label) else 0
                    if info.get("tag") == "textarea":
                        score += 2
                    signature = (info.get("x"), info.get("y"), info.get("w"), info.get("h"))
                    if not any(row[2] == signature for row in candidates):
                        candidates.append((score, node, signature))
                except Exception:
                    continue
        if not candidates:
            raise GateError("NOTEBOOK_PYTHON_PROMPT_INPUT_NOT_FOUND")
        candidates.sort(key=lambda row: row[0], reverse=True)
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            raise GateError("NOTEBOOK_PYTHON_PROMPT_INPUT_AMBIGUOUS")
        target = candidates[0][1]
        try:
            await target.fill(goal, timeout=12000)
            typed = await target.evaluate("(el) => ('value' in el ? el.value : el.innerText || '')")
        except Exception as exc:
            raise GateError("NOTEBOOK_PYTHON_PROMPT_FILL_FAILED_" + type(exc).__name__) from None
        if not isinstance(typed, str) or len(typed.strip()) < max(40, int(len(goal.strip()) * .85)):
            raise GateError("NOTEBOOK_PYTHON_PROMPT_FILL_NOT_VERIFIED")

    async def run(self, start_url: str, goal: str, *,
                  upload_path: str | None = None,
                  allow_facebook_publish: bool = False,
                  max_steps: int = 20,
                  lessons: list[dict] | None = None):
        """Execute only fixed Python-rule UI actions on the existing page.

        Only fixed Python rules are available. Unknown task semantics or ambiguous controls are blocked.
        """
        del allow_facebook_publish, lessons
        if not 1 <= max_steps <= 50:
            raise ValueError("max_steps must be in range 1..50")
        Chrome._allow(start_url)
        if self.page.is_closed():
            raise RuntimeError("KAGGLE_EXISTING_PAGE_CLOSED")
        if self.page.url != start_url:
            await self.page.goto(start_url, wait_until="domcontentloaded", timeout=45000)
        await self._login_gate()

        if re.match(r"^\s*(?:publish|post now|share|delete|đăng bài|đăng ngay|chia sẻ)", goal, re.I):
            raise GateError("FACEBOOK_PUBLISH_REQUIRES_SEPARATE_APPROVAL_GATE")
        if _INSPECT_ONLY.search(goal.strip()):
            return {"page_url": self.page.url, "body_text": await self.inspect(),
                    "download_path": None, "steps": 0}

        host = (self.page.url.split("/", 3)[2] if "://" in self.page.url else "").lower()
        generate = bool(_GENERATE.search(goal))
        download_path = None
        if upload_path:
            source = Path(upload_path)
            if not source.is_file() or source.suffix.lower() != ".mp4":
                raise GateError("NOTEBOOK_UPLOAD_SOURCE_NOT_VERIFIED")
            # Upload is enabled only after a fresh live DOM exposes the upload menu.
            snapshot = await self._snapshot()
            upload_control = self._select_control(snapshot["controls"], "upload files", mode="control")
            await self._click_control(upload_control, allow_menu=True)
            snapshot = await self._snapshot()
            if not any(re.search(r"upload files|tải tệp lên", self._label(c), re.I)
                       for c in snapshot["controls"]):
                raise GateError("NOTEBOOK_UPLOAD_MENU_NOT_VERIFIED")
            inputs = self.page.locator('input[type="file"]')
            count = await inputs.count()
            if count != 1:
                raise GateError("NOTEBOOK_UPLOAD_INPUT_AMBIGUOUS")
            await inputs.first.set_input_files(str(source), timeout=15000)
            await self._login_gate()

        if generate:
            await self._fill_prompt(goal)
            baseline = await self.page.locator("body").inner_text(timeout=12000)
            snapshot = await self._snapshot()
            submit = self._select_control(snapshot["controls"], goal, mode="generate")
            await self._login_gate()
            await self._click_control(submit)
            for step in range(1, max_steps + 1):
                await asyncio.sleep(5)
                await self._login_gate()
                body = await self.page.locator("body").inner_text(timeout=12000)
                if body == baseline:
                    continue
                snapshot = await self._snapshot()
                try:
                    button = self._select_control(snapshot["controls"], "download video", mode="download")
                except GateError as exc:
                    if str(exc) == "NOTEBOOK_PYTHON_CONTROL_AMBIGUOUS":
                        raise
                    continue
                try:
                    self.downloads_dir.mkdir(parents=True, exist_ok=True)
                    async with self.page.expect_download(timeout=30000) as event:
                        await self._click_control(button)
                    item = await event.value
                    destination = self.downloads_dir / Path(item.suggested_filename).name
                    await item.save_as(str(destination))
                except Exception as exc:
                    raise GateError("NOTEBOOK_DOWNLOAD_FAILED_" + type(exc).__name__) from None
                if not destination.is_file() or destination.stat().st_size <= 0:
                    raise GateError("NOTEBOOK_DOWNLOAD_FILE_EMPTY")
                download_path = str(destination)
                return {"page_url": self.page.url, "body_text": await self.inspect(),
                        "download_path": download_path, "steps": step}
            raise GateError("NOTEBOOK_VIDEO_RESULT_NOT_VERIFIED")

        mode = "download" if _DOWNLOAD.search(goal) else "control"
        snapshot = await self._snapshot()
        target = self._select_control(snapshot["controls"], goal, mode=mode)
        await self._click_control(target, allow_menu=(mode == "control"))
        await asyncio.sleep(0.5)
        await self._login_gate()
        after = await self._snapshot()
        if not isinstance(after, dict) or not after.get("controls"):
            raise GateError("NOTEBOOK_POST_ACTION_STATE_UNVERIFIED")
        return {"page_url": self.page.url, "body_text": await self.inspect(),
                "download_path": download_path, "steps": 1}
