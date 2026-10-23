"""Telegram owner gate. No Bot API callbacks from strangers can approve posts."""
from __future__ import annotations
from typing import Any
from pathlib import Path
import requests

from .core import GateError, Job, Store


class Telegram:
    def __init__(self, token: str, owner_id: int, store: Store, session: Any = None) -> None:
        if not token or not owner_id:
            raise ValueError("Telegram bot token and owner Telegram numeric ID are required")
        self.token, self.owner_id, self.store = token, owner_id, store
        self.http = session or requests.Session()
        self.endpoint = f"https://api.telegram.org/bot{token}/"

    def _call(self, method: str, *, data: dict[str, Any] | None = None,
              files: dict[str, Any] | None = None, timeout: int = 65) -> Any:
        # The Bot API endpoint contains the token. Never propagate requests'
        # exception text, HTTPError strings, or provider descriptions into job
        # logs / Telegram alerts: those strings can include the authenticated URL.
        method_code = "".join(ch for ch in method.upper() if ch.isalnum())[:32]
        try:
            r = self.http.post(self.endpoint + method, data=data, files=files,
                               timeout=timeout)
        except requests.RequestException as exc:
            raise RuntimeError(
                f"TELEGRAM_{method_code}_NETWORK_ERROR_{type(exc).__name__}"
            ) from None
        status = getattr(r, "status_code", None)
        if not isinstance(status, int) or not 200 <= status < 300:
            suffix = str(status) if isinstance(status, int) else "INVALID"
            raise RuntimeError(f"TELEGRAM_{method_code}_HTTP_{suffix}")
        try:
            payload = r.json()
        except (ValueError, TypeError):
            raise RuntimeError(f"TELEGRAM_{method_code}_INVALID_RESPONSE") from None
        if not isinstance(payload, dict):
            raise RuntimeError(f"TELEGRAM_{method_code}_INVALID_RESPONSE")
        if payload.get("ok") is not True:
            # The provider's description is intentionally omitted; it is
            # untrusted and is not needed to decide whether to fail closed.
            raise RuntimeError(f"TELEGRAM_{method_code}_REJECTED")
        if "result" not in payload:
            raise RuntimeError(f"TELEGRAM_{method_code}_RESULT_MISSING")
        return payload["result"]

    def error(self, job: Job, message: str) -> None:
        self._call("sendMessage", data={"chat_id": self.owner_id,
              "text": f"Tool System V2 LỖI — {job.page} / {job.id}\n{message[:2500]}"})

    def send_for_review(self, job: Job) -> str:
        if not job.video_path or not Path(job.video_path).is_file() or not job.video_sha:
            raise GateError("Cannot review without real video")
        prefix = job.video_sha[:16]
        keys = {"inline_keyboard": [[
            {"text": "✅ Duyệt", "callback_data": f"approve:{job.id}:{prefix}"},
            {"text": "✏️ Sửa lại", "callback_data": f"revise:{job.id}:{prefix}"},
        ]]}
        import json
        data = {"chat_id": self.owner_id,
                "caption": f"Tool System V2: {job.page}\nJob: {job.id}\nMP4: {prefix}\nChỉ duyệt nếu đúng video.",
                "reply_markup": json.dumps(keys, ensure_ascii=False)}
        with open(job.video_path, "rb") as f:
            response = self._call("sendVideo", data=data,
                                  files={"video": (Path(job.video_path).name, f, "video/mp4")})
        return str(response["message_id"])

    def handle_callback(self, callback: dict[str, Any]) -> str:
        sender = callback.get("from", {}).get("id")
        if sender != self.owner_id:
            raise GateError("Not the configured owner")
        msg = callback.get("message", {})
        if msg.get("chat", {}).get("id") != self.owner_id:
            raise GateError("Callback not sent in owner's private chat")
        try:
            action, jid, prefix = callback["data"].split(":", 2)
        except (KeyError, ValueError):
            raise GateError("Invalid callback")
        if action not in {"approve", "revise"}:
            raise GateError("Unsupported review action")
        job = self.store.get(jid)
        if (job.stage != "WAITING_APPROVAL" or str(msg.get("message_id")) != job.review_message_id
            or not job.video_sha or prefix != job.video_sha[:16]):
            raise GateError("Outdated review or wrong video SHA")
        if action == "approve":
            self.store.move(jid, "APPROVED", note="Owner approved exact SHA via Telegram",
                            owner_approved_sha=job.video_sha)
            result = "Đã duyệt đúng video. Chờ lịch đăng."
        else:
            self.store.move(jid, "REVISION_REQUESTED", note="Owner requested video revision",
                            owner_approved_sha=None, feedback="Owner requested revision")
            result = "Đã nhận yêu cầu sửa. Hãy trả lời trực tiếp vào tin nhắn video này bằng mô tả chỉnh sửa."
        self._call("answerCallbackQuery", data={"callback_query_id": callback["id"], "text": result})
        return action
