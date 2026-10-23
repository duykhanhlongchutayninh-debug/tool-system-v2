"""Owner-only Telegram approval polling in the existing Kaggle runtime.

No webhook, hosted endpoint, extra Chrome or separate long-lived process.
Every callback is checked against owner, review message ID, job and video SHA.
"""
from __future__ import annotations

from threading import Event
from typing import Any, Callable
import re

from .infra_telegram import get_review_updates
from .telegram import Telegram


JOB_ID_RE = re.compile(r"^[a-f0-9]{16}$")


class TelegramApprovalPoller:
    def __init__(
        self,
        token: str,
        owner_chat_id: int,
        store: Any,
        *,
        telegram: Telegram | None = None,
        update_fetcher: Callable[..., list[dict[str, Any]]] | None = None,
        notify: Callable[[str], None] | None = None,
        on_event: Callable[[str], None] | None = None,
        on_approved: Callable[[str], None] | None = None,
        timeout: int = 8,
    ) -> None:
        if not token or owner_chat_id <= 0:
            raise ValueError("TELEGRAM_APPROVAL_OWNER_CONFIGURATION_INVALID")
        self.token = token
        self.owner_chat_id = int(owner_chat_id)
        self.store = store
        self.telegram = telegram or Telegram(token, self.owner_chat_id, store)
        self.update_fetcher = update_fetcher or get_review_updates
        self.notify = notify
        self.on_event = on_event or (lambda message: print(message, flush=True))
        self.on_approved = on_approved
        self.timeout = timeout
        self.offset: int | None = None

    def _event(self, code: str) -> None:
        # Only fixed, machine-readable codes leave the poller.
        safe = "".join(char if char.isalnum() or char in "_=" else "_" for char in code)[:120]
        self.on_event("TOOL_SYSTEM_TELEGRAM_APPROVAL=" + safe)

    def _ack(self, callback: dict[str, Any], text: str) -> None:
        callback_id = callback.get("id")
        if not isinstance(callback_id, str) or not callback_id:
            return
        try:
            self.telegram._call("answerCallbackQuery", data={
                "callback_query_id": callback_id, "text": text[:180],
            })
        except Exception:
            # The durable job state is authoritative if the toast request fails.
            pass

    def _is_owner_private_callback(self, callback: dict[str, Any]) -> bool:
        sender = callback.get("from", {})
        message = callback.get("message", {})
        chat = message.get("chat", {}) if isinstance(message, dict) else {}
        try:
            return (int(sender.get("id", -1)) == self.owner_chat_id
                    and int(chat.get("id", -1)) == self.owner_chat_id
                    and chat.get("type") == "private")
        except (AttributeError, TypeError, ValueError):
            return False

    @staticmethod
    def _is_applied(job, action: str, prefix: str, message_id: str) -> bool:
        if (str(job.review_message_id or "") != message_id or not job.video_sha
                or not job.video_sha.startswith(prefix)):
            return False
        if action == "approve":
            return job.stage == "APPROVED" and job.owner_approved_sha == job.video_sha
        if action == "revise":
            return job.stage == "REVISION_REQUESTED" and job.owner_approved_sha is None
        return False

    def _ensure_cloud_checkpoint(self) -> bool:
        checkpoint = getattr(self.store, "checkpoint", None)
        if checkpoint is None:
            return True
        try:
            checkpoint.backup(self.store.path)
        except Exception:
            return False
        return True

    def _finish_applied_callback(self, callback: dict[str, Any], action: str,
                                 *, recovered: bool, acknowledge: bool = True,
                                 notify_owner: bool = True) -> str:
        message = ("✅ Đã ghi nhận duyệt đúng SHA video. Trạng thái lưu bền vững."
                   if action == "approve" else
                   "✏️ Đã ghi nhận yêu cầu sửa. Hãy Reply trực tiếp vào tin nhắn video này "
                   "và ghi mô tả chỉnh sửa; sau đó chạy Bước 06.")
        if acknowledge:
            self._ack(callback, message)
        self._event(("STATE_CHECKPOINT_RECOVERED_" if recovered else "") + action.upper())
        if action == "approve" and self.on_approved:
            try:
                parts = str(callback.get("data", "")).split(":", 2)
                if len(parts) == 3 and JOB_ID_RE.fullmatch(parts[1]):
                    self.on_approved(parts[1])
                    self._event("SCHEDULE_TRIGGER_ENQUEUED")
                else:
                    self._event("SCHEDULE_TRIGGER_SKIPPED_INVALID_JOB_ID")
            except Exception as exc:
                self._event("SCHEDULE_TRIGGER_FAILED_" + type(exc).__name__.upper())
                if notify_owner and self.notify:
                    try:
                        self.notify("Không khởi chạy được lịch Meta cho video vừa duyệt; kiểm tra log Tool System V2.")
                    except Exception:
                        pass
        if notify_owner and self.notify:
            try:
                self.notify(message)
            except Exception:
                pass
        return "CONSUMED"

    def _process_owner_reply(self, message: dict[str, Any]) -> str:
        """Save a text reply only after owner clicked 'Sửa lại' for that exact video."""
        sender = message.get("from", {})
        chat = message.get("chat", {})
        try:
            owner_private = (
                int(sender.get("id", -1)) == self.owner_chat_id
                and int(chat.get("id", -1)) == self.owner_chat_id
                and chat.get("type") == "private"
            )
        except (AttributeError, TypeError, ValueError):
            owner_private = False
        if not owner_private:
            self._event("REVISION_REPLY_REJECTED_NON_OWNER")
            return "CONSUMED"

        reply_to = message.get("reply_to_message")
        if not isinstance(reply_to, dict) or reply_to.get("message_id") is None:
            return "CONSUMED"
        reply_message_id = str(reply_to.get("message_id"))
        text = str(message.get("text") or message.get("caption") or "").strip()
        if not text or text.startswith("/"):
            return "CONSUMED"

        try:
            matches = [job for job in self.store.list_jobs()
                       if str(job.review_message_id or "") == reply_message_id]
        except Exception:
            return "RETRY_LATER"
        if len(matches) != 1:
            return "CONSUMED"
        job = matches[0]
        if job.stage != "REVISION_REQUESTED":
            # Require the explicit "Sửa lại" button, so ordinary comments
            # cannot silently reject a video awaiting owner approval.
            return "CONSUMED"
        if len(text) > 2000:
            self._event("REVISION_REPLY_REJECTED_TOO_LONG")
            if self.notify:
                try:
                    self.notify("Mô tả sửa video quá dài; hãy gửi tối đa 2.000 ký tự.")
                except Exception:
                    pass
            return "CONSUMED"
        try:
            updated = self.store.update_revision_feedback(job.id, text)
            if updated.feedback != text or updated.stage != "REVISION_REQUESTED":
                return "RETRY_LATER"
        except Exception:
            # A local write may have succeeded before the cloud checkpoint
            # failed. Keep the Telegram update pending and retry checkpoint.
            return "RETRY_LATER"
        self._event("REVISION_FEEDBACK_SAVED")
        if self.notify:
            try:
                self.notify("✅ Đã lưu yêu cầu chỉnh sửa cho job " + job.id +
                            ". Nhấn Bước 06 để tạo lại kịch bản theo phản hồi.")
            except Exception:
                pass
        return "CONSUMED"

    def process_update(self, update: dict[str, Any]) -> str:
        """Process one update; return RETRY_LATER without advancing its offset."""
        if not isinstance(update, dict):
            return "CONSUMED"
        message = update.get("message")
        if isinstance(message, dict):
            return self._process_owner_reply(message)
        callback = update.get("callback_query")
        if not isinstance(callback, dict):
            return "CONSUMED"
        if not self._is_owner_private_callback(callback):
            self._ack(callback, "Không có quyền thao tác trên bài duyệt này.")
            self._event("REJECTED_NON_OWNER")
            return "CONSUMED"

        try:
            action, job_id, prefix = str(callback.get("data", "")).split(":", 2)
            message_id = str(callback["message"]["message_id"])
            if (action not in {"approve", "revise"} or not job_id
                    or len(prefix) != 16
                    or not all(ch in "0123456789abcdef" for ch in prefix)
                    or not isinstance(callback.get("id"), str)
                    or not callback.get("id")):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            self._ack(callback, "Nút duyệt không hợp lệ hoặc đã cũ.")
            self._event("REJECTED_INVALID_CALLBACK")
            return "CONSUMED"
        try:
            job = self.store.get(job_id)
        except KeyError:
            self._ack(callback, "Không tìm thấy job cho nút duyệt này.")
            self._event("REJECTED_UNKNOWN_JOB")
            return "CONSUMED"
        except Exception:
            # A temporary local database error must not consume a valid update.
            return "RETRY_LATER"

        if not job.video_sha or not job.video_sha.startswith(prefix):
            self._ack(callback, "SHA video không khớp; hãy dùng tin duyệt mới nhất.")
            self._event("REJECTED_VIDEO_SHA_MISMATCH")
            return "CONSUMED"

        # sendVideo may be visible on Telegram moments before WAITING_APPROVAL
        # is checkpointed. Keep this update unacknowledged until exact receipt
        # state appears; do not reject a valid, very fast owner click.
        if job.stage == "VIDEO_READY" and not job.review_message_id:
            return "RETRY_LATER"

        if self._is_applied(job, action, prefix, message_id):
            if not self._ensure_cloud_checkpoint():
                return "RETRY_LATER"
            return self._finish_applied_callback(
                callback, action, recovered=False, notify_owner=False
            )

        if job.stage != "WAITING_APPROVAL" or str(job.review_message_id or "") != message_id:
            self._ack(callback, "Nút duyệt cũ hoặc không khớp tin nhắn video hiện tại.")
            self._event("REJECTED_STALE_REVIEW")
            return "CONSUMED"

        try:
            applied_action = self.telegram.handle_callback(callback)
        except Exception:
            # A move may have succeeded locally before the cloud checkpoint or
            # callback toast failed. Verify/persist the exact resulting state
            # before consuming this update; otherwise retry the same update.
            current = self.store.get(job_id)
            if self._is_applied(current, action, prefix, message_id):
                if not self._ensure_cloud_checkpoint():
                    return "RETRY_LATER"
                return self._finish_applied_callback(callback, action, recovered=True)
            self._ack(callback, "Không thể xác minh thao tác; hãy kiểm tra trạng thái job.")
            self._event("REJECTED_OR_UNVERIFIED_CALLBACK")
            return "CONSUMED"

        # Telegram.handle_callback already answered this query after the
        # durable state transition; do not send a duplicate answerCallbackQuery.
        return self._finish_applied_callback(
            callback, applied_action, recovered=False, acknowledge=False
        )

    def poll_once(self) -> str:
        updates = self.update_fetcher(
            self.token, offset=self.offset, timeout=self.timeout
        )
        for update in updates:
            update_id = update.get("update_id") if isinstance(update, dict) else None
            if not isinstance(update_id, int) or update_id < 0:
                continue
            result = self.process_update(update)
            if result == "RETRY_LATER":
                # No offset increase: Telegram will replay this exact update.
                return result
            self.offset = max(self.offset or 0, update_id + 1)
        return "POLLED"

    def run_forever(self, stop_event: Event) -> None:
        last_error = None
        while not stop_event.is_set():
            try:
                status = self.poll_once()
                last_error = None
                if status == "RETRY_LATER":
                    stop_event.wait(1.0)
            except Exception as exc:
                code = type(exc).__name__
                if code != last_error:
                    self._event("POLL_RETRY_" + code)
                    if self.notify:
                        try:
                            self.notify("Telegram chưa thể kiểm tra nút duyệt: " + code)
                        except Exception:
                            pass
                last_error = code
                stop_event.wait(5.0)
