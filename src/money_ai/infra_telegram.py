"""Telegram infrastructure notifier: send only after a verified owner handshake.

No extra worker, server, webhook or hosting. Calls the existing Telegram Bot
API directly from Kaggle. Token stays in Kaggle Secrets or process memory.
Never display the Bot API URL because it embeds the bot token.
"""
from __future__ import annotations

import os
import re
from typing import Any

import requests

_TOKEN = re.compile(r"^[0-9]{5,20}:[A-Za-z0-9_-]{20,150}$")
_CHAT = re.compile(r"^[0-9]{4,20}$")
BOT_KEY = "TOOL_SYSTEM_TELEGRAM_BOT_TOKEN"
CHAT_KEY = "TOOL_SYSTEM_TELEGRAM_OWNER_CHAT_ID"


class TelegramSetupError(RuntimeError):
    """Only static error codes; do not expose provider response or bot token."""


def _api(token: str, method: str, *, data=None, session=None,
         request_timeout: int = 12) -> Any:
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise TelegramSetupError("TELEGRAM_BOT_TOKEN_INVALID")
    http = session or requests
    try:
        response = http.post(
            "https://api.telegram.org/bot" + token + "/" + method,
            json=data or {}, timeout=request_timeout,
        )
        if response.status_code != 200:
            raise TelegramSetupError("TELEGRAM_" + method.upper() +
                                     "_HTTP_" + str(response.status_code))
        payload = response.json()
        if payload.get("ok") is not True:
            raise TelegramSetupError("TELEGRAM_" + method.upper() + "_REJECTED")
        return payload.get("result")
    except TelegramSetupError:
        raise
    except (requests.RequestException, ValueError, TypeError):
        raise TelegramSetupError("TELEGRAM_" + method.upper() + "_NETWORK_OR_RESPONSE_ERROR") from None


def get_callback_updates(token: str, *, offset: int | None = None,
                        timeout: int = 8, session=None) -> list[dict[str, Any]]:
    """Long-poll only Telegram inline-button callbacks for the verified owner flow.

    This does not set/delete webhooks. A webhook-configured bot fails closed and
    can continue to use its existing configuration without being mutated here.
    """
    if not isinstance(timeout, int) or not 1 <= timeout <= 20:
        raise TelegramSetupError("TELEGRAM_CALLBACK_POLL_TIMEOUT_INVALID")
    if offset is not None and (not isinstance(offset, int) or offset < 0):
        raise TelegramSetupError("TELEGRAM_CALLBACK_POLL_OFFSET_INVALID")
    data: dict[str, Any] = {
        "timeout": timeout,
        "allowed_updates": ["callback_query"],
    }
    if offset is not None:
        data["offset"] = offset
    result = _api(token, "getUpdates", data=data, session=session,
                  request_timeout=timeout + 5)
    if not isinstance(result, list):
        raise TelegramSetupError("TELEGRAM_CALLBACK_UPDATES_NOT_LIST")
    return result


def get_review_updates(token: str, *, offset: int | None = None,
                       timeout: int = 8, session=None) -> list[dict[str, Any]]:
    """Bounded polling for owner review callbacks and replies to review videos."""
    if not isinstance(timeout, int) or not 1 <= timeout <= 20:
        raise TelegramSetupError("TELEGRAM_REVIEW_POLL_TIMEOUT_INVALID")
    if offset is not None and (not isinstance(offset, int) or offset < 0):
        raise TelegramSetupError("TELEGRAM_REVIEW_POLL_OFFSET_INVALID")
    data: dict[str, Any] = {
        "timeout": timeout,
        "allowed_updates": ["callback_query", "message"],
    }
    if offset is not None:
        data["offset"] = offset
    result = _api(token, "getUpdates", data=data, session=session,
                  request_timeout=timeout + 5)
    if not isinstance(result, list):
        raise TelegramSetupError("TELEGRAM_REVIEW_UPDATES_NOT_LIST")
    return result


def discover_owner_chat(token: str, *, session=None) -> str:
    """Use exactly one private /start sender or refuse ambiguity.

    Does not advance Telegram update offset or change a bot webhook. If the
    existing bot has a webhook, the owner can provide their numeric Chat ID.
    """
    updates = _api(token, "getUpdates", data={"limit": 50, "timeout": 0},
                   session=session)
    if not isinstance(updates, list):
        raise TelegramSetupError("TELEGRAM_UPDATES_NOT_LIST")
    private_ids = set()
    for update in updates:
        msg = update.get("message", {}) if isinstance(update, dict) else {}
        if not isinstance(msg, dict):
            continue
        chat = msg.get("chat", {})
        sender = msg.get("from", {})
        if (isinstance(chat, dict) and isinstance(sender, dict)
                and chat.get("type") == "private"
                and sender.get("id") == chat.get("id")
                and str(msg.get("text", "")).split(" ", 1)[0] == "/start"):
            private_ids.add(str(chat.get("id")))
    if len(private_ids) != 1:
        raise TelegramSetupError(
            "TELEGRAM_OWNER_ID_REQUIRED" if not private_ids
            else "TELEGRAM_MULTIPLE_PRIVATE_CHATS_REQUIRE_OWNER_ID")
    return private_ids.pop()


def verify_owner(token: str, chat_id: str = "", *, session=None) -> str:
    """Only accept a Telegram sendMessage receipt from the intended owner."""
    getme = _api(token, "getMe", session=session)
    if not isinstance(getme, dict) or not getme.get("is_bot"):
        raise TelegramSetupError("TELEGRAM_NOT_A_BOT")
    chosen = str(chat_id or "").strip() or discover_owner_chat(token, session=session)
    if not _CHAT.fullmatch(chosen):
        raise TelegramSetupError("TELEGRAM_PRIVATE_CHAT_ID_INVALID")
    receipt = _api(token, "sendMessage", data={
        "chat_id": int(chosen),
        "text": "✅ Tool System V2: Kết nối hạ tầng Telegram thành công. Đây là tin xác minh từ Kaggle.",
    }, session=session)
    if (not isinstance(receipt, dict) or
            not isinstance(receipt.get("chat"), dict) or
            str(receipt["chat"].get("id")) != chosen or
            receipt["chat"].get("type") != "private" or
            not isinstance(receipt.get("message_id"), int)):
        raise TelegramSetupError("TELEGRAM_OWNER_RECEIPT_UNVERIFIED")
    return chosen


def configured(env=None) -> tuple[str, str] | None:
    env = env if env is not None else os.environ
    token = env.get(BOT_KEY) or env.get("MONEY_TELEGRAM_BOT_TOKEN")
    chat = (env.get(CHAT_KEY) or env.get("TOOL_SYSTEM_TELEGRAM_OWNER_ID")
            or env.get("MONEY_TELEGRAM_OWNER_CHAT_ID"))
    if not token or not chat:
        return None
    if not _TOKEN.fullmatch(token) or not _CHAT.fullmatch(str(chat)):
        return None
    return token, str(chat)


def notify(area: str, message: str, *, env=None, session=None) -> str:
    """Return SENT/NOT_CONFIGURED/FAILED; no false acknowledgement."""
    keys = configured(env)
    if keys is None:
        return "NOT_CONFIGURED"
    token, chat = keys
    try:
        receipt = _api(token, "sendMessage", data={
            "chat_id": int(chat), "text": "Tool System V2 | " + str(area)[:40] +
            "\n" + str(message)[:1400],
        }, session=session)
        if (not isinstance(receipt, dict) or
                str((receipt.get("chat") or {}).get("id")) != chat or
                not isinstance(receipt.get("message_id"), int)):
            return "FAILED"
        return "SENT"
    except TelegramSetupError:
        return "FAILED"
