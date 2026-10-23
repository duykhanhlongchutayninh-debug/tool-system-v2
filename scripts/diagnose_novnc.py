"""Diagnose the full local noVNC connection without restarting any services.

Checks actual RFB greeting on 5900 and actual WebSocket upgrade on 6080.
A passing local test does NOT prove the Android Kaggle gateway path.
Never display URLs, authentication tokens, cookies or log contents.
"""
from __future__ import annotations
import base64
import os
from pathlib import Path
import socket
from types import SimpleNamespace
import runpy

# Read the active release script directly. The Notebook may still cache an
# older scripts.kaggle_cpu_desktop import after Github watcher updates code.
desk = SimpleNamespace(**runpy.run_path(
    str(Path(__file__).with_name("kaggle_cpu_desktop.py"))
))


def rfb_ready() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 5900), timeout=3) as client:
            client.settimeout(3)
            header = client.recv(12)
            return header.startswith(b"RFB ")
    except OSError:
        return False


def local_ws_ready() -> bool:
    """Full RFC6455 Upgrade handshake via websockify, not a plain HTML GET."""
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        "GET /websockify HTTP/1.1\r\n"
        "Host: 127.0.0.1:6080\r\n"
        "Connection: Upgrade\r\n"
        "Upgrade: websocket\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Sec-WebSocket-Key: " + key + "\r\n"
        "Sec-WebSocket-Protocol: binary\r\n\r\n"
    ).encode("ascii")
    try:
        with socket.create_connection(("127.0.0.1", 6080), timeout=4) as client:
            client.settimeout(4)
            client.sendall(request)
            answer = client.recv(1024)
            status = answer.split(b"\r\n", 1)[0]
            return b" 101 " in status
    except OSError:
        return False


def patch_ready() -> bool:
    root = desk._novnc_webroot()
    if root is None:
        return False
    html = (root / "vnc.html").read_text(encoding="utf-8", errors="replace")
    js = root / "app" / "tool_system_proxy_path.js"
    return ('tool_system_proxy_path.js?v=2' in html
            and js.is_file()
            and "url.searchParams.set('path', wanted)" in
            js.read_text(encoding="utf-8", errors="replace"))


def diagnostic() -> dict[str, str]:
    results = {
        "CHROME_CDP": "PASS" if desk._cdp_alive() else "FAIL",
        "X11VNC_RFB": "PASS" if rfb_ready() else "FAIL",
        "LOCAL_WEBSOCKET": "PASS" if local_ws_ready() else "FAIL",
        "NOVNC_HTTP": "PASS" if desk._novnc_http_alive() else "FAIL",
        "PROXY_JS": "PASS" if patch_ready() else "FAIL",
        "ANDROID_GATEWAY": "NOT_VERIFIED",
    }
    for label, result in results.items():
        print("TOOL_SYSTEM_DIAG_" + label + "=" + result)
    return results


if __name__ == "__main__":
    diagnostic()
