"""Tool System V2 Kaggle dashboard: Chrome/screen controls and business Steps 05-07.

Run inside the Kaggle notebook:
    from scripts.tool_system_dashboard import show_dashboard
    show_dashboard()

Secrets are held in the current kernel's environment only, never saved to files.
The dashboard does not run business jobs automatically.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
CDP_URL = "http://127.0.0.1:9222"
SCREEN_SUFFIX = "/proxy/6080/vnc.html"
SCREEN_HOST = "kkb-production.jupyter-proxy.kaggle.net"


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.6):
            return True
    except OSError:
        return False


def _screen_url(raw: str) -> str:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 4096:
        raise ValueError("KAGGLE_PROXY_URL_REQUIRED")
    parsed = urlsplit(raw.strip())
    if (parsed.scheme != "https" or parsed.hostname != SCREEN_HOST
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("KAGGLE_PROXY_URL_INVALID")
    parts = parsed.path.strip("/").split("/")
    if (len(parts) != 4 or parts[0] != "k" or parts[3] != "proxy"
            or not all(re.fullmatch(r"[A-Za-z0-9._-]+", item) for item in parts[1:3])):
        raise ValueError("KAGGLE_PROXY_URL_INVALID")
    return raw.strip().rstrip("/") + SCREEN_SUFFIX


def _screen_url_from_server(base_url: str) -> str:
    raw = str(base_url or "").strip()
    if not raw:
        raise ValueError("KAGGLE_BASE_URL_MISSING")
    if "://" in raw:
        parsed = urlsplit(raw)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("KAGGLE_BASE_URL_INVALID")
        path = parsed.path
    else:
        path = raw
    parts = path.strip("/").split("/")
    if len(parts) < 4 or parts[0] != "k" or parts[3] != "proxy":
        raise ValueError("KAGGLE_BASE_URL_INVALID")
    if not all(re.fullmatch(r"[A-Za-z0-9._-]+", item) for item in parts[1:3]):
        raise ValueError("KAGGLE_BASE_URL_INVALID")
    base = "https://" + SCREEN_HOST + "/" + "/".join(parts[:4])
    return _screen_url(base)


def _discover_screen_url() -> str | None:
    try:
        from jupyter_server.serverapp import list_running_servers
        for server in list(list_running_servers()):
            if isinstance(server, dict):
                try:
                    return _screen_url_from_server(server.get("base_url", ""))
                except (TypeError, ValueError):
                    continue
    except Exception:
        pass
    for name in ("CODE_SANDBOX_URL", "KAGGLE_PROXY_URL", "KAGGLE_JUPYTER_PROXY_URL"):
        candidate = os.environ.get(name, "")
        if candidate:
            try:
                return _screen_url(candidate)
            except ValueError:
                pass
    return None


def show_dashboard():
    """Display the real control buttons in the currently running Kaggle notebook."""
    if not Path("/kaggle").is_dir():
        raise RuntimeError("TOOL_SYSTEM_DASHBOARD_REQUIRES_KAGGLE")

    import ipywidgets as widgets
    from IPython.display import display

    output = widgets.Output(layout=widgets.Layout(width="100%"))
    chrome_status = widgets.HTML(value="<b>Chrome:</b> chưa được xác minh trong dashboard.")
    screen_status = widgets.HTML(value="")
    config_status = widgets.HTML(value="<b>Cấu hình:</b> chưa nạp cấu hình Supabase/Telegram cho dự án mới.")
    step05_status = widgets.HTML(value="")
    step06_status = widgets.HTML(value="")
    step07_status = widgets.HTML(value="")

    supabase_url = widgets.Text(
        description="Supabase URL:",
        placeholder="https://<project-ref>.supabase.co",
        layout=widgets.Layout(width="100%"),
    )
    supabase_key = widgets.Password(
        description="Service key:",
        placeholder="Khóa của Supabase RIÊNG cho Tool System V2",
        layout=widgets.Layout(width="100%"),
    )
    telegram_token = widgets.Password(
        description="Telegram bot:",
        placeholder="Token bot của Tool System V2 (nếu dùng Bước 07)",
        layout=widgets.Layout(width="100%"),
    )
    telegram_chat = widgets.Text(
        description="Owner chat ID:",
        placeholder="Chat ID dạng số (nếu dùng Bước 07)",
        layout=widgets.Layout(width="100%"),
    )
    save_config_button = widgets.Button(
        description="Nạp cấu hình vào phiên Kaggle",
        button_style="info",
        layout=widgets.Layout(width="100%"),
    )
    chrome_button = widgets.Button(
        description="Khởi động / kiểm tra Chrome",
        button_style="success",
        layout=widgets.Layout(width="100%"),
    )
    backup_button = widgets.Button(
        description="Sao lưu hồ sơ Chrome (có tạm dừng Chrome)",
        button_style="warning",
        layout=widgets.Layout(width="100%"),
        disabled=True,
    )
    screen_url_input = widgets.Password(
        description="URL Kaggle:",
        placeholder="URL hiện tại từ Copy VS Code Compatible URL, kết thúc bằng /proxy",
        layout=widgets.Layout(width="100%"),
    )
    screen_link = widgets.HTML(
        value="<p>Đang tìm đường dẫn màn hình của phiên Kaggle hiện tại...</p>",
        layout=widgets.Layout(width="100%"),
    )

    step05_button = widgets.Button(
        description="Bước 05 — lấy và xác minh MP4 nguồn",
        button_style="success",
        layout=widgets.Layout(width="100%"),
        disabled=True,
    )
    step06_button = widgets.Button(
        description="Bước 06 — Gemini xem/nghe nguồn, viết kịch bản",
        button_style="info",
        layout=widgets.Layout(width="100%"),
        disabled=True,
    )
    step07_button = widgets.Button(
        description="Bước 07 — Meta AI tạo MP4, gửi Telegram duyệt",
        button_style="success",
        layout=widgets.Layout(width="100%"),
        disabled=True,
    )

    running = {"worker": False}

    def _load_existing_config():
        # Import ONLY explicitly named Tool System V2 secrets; never fall back to Money AI secrets.
        try:
            from kaggle_secrets import UserSecretsClient
            secrets = UserSecretsClient()
        except Exception:
            return
        for name, widget in (
            ("TOOL_SYSTEM_SUPABASE_URL", supabase_url),
            ("TOOL_SYSTEM_SUPABASE_SERVICE_ROLE_KEY", supabase_key),
            ("TOOL_SYSTEM_TELEGRAM_BOT_TOKEN", telegram_token),
            ("TOOL_SYSTEM_TELEGRAM_OWNER_CHAT_ID", telegram_chat),
        ):
            if os.environ.get(name):
                widget.value = os.environ[name]
                continue
            try:
                value = secrets.get_secret(name)
            except Exception:
                value = ""
            if isinstance(value, str) and value.strip():
                widget.value = value.strip()

    def _refresh_buttons():
        chrome_ok = _port_open(9222)
        config_ok = bool(
            os.environ.get("TOOL_SYSTEM_SUPABASE_URL", "").strip()
            and os.environ.get("TOOL_SYSTEM_SUPABASE_SERVICE_ROLE_KEY", "").strip()
        )
        telegram_ok = bool(
            os.environ.get("TOOL_SYSTEM_TELEGRAM_BOT_TOKEN", "").strip()
            and os.environ.get("TOOL_SYSTEM_TELEGRAM_OWNER_CHAT_ID", "").strip().isdigit()
        )
        busy = running["worker"]
        step05_button.disabled = busy or not (chrome_ok and config_ok)
        step06_button.disabled = busy or not (chrome_ok and config_ok)
        step07_button.disabled = busy or not (chrome_ok and config_ok and telegram_ok)
        backup_button.disabled = busy or not (chrome_ok and config_ok)
        chrome_status.value = (
            "<b>Chrome CDP:</b> PASS (127.0.0.1:9222 đang mở)."
            if chrome_ok else
            "<b>Chrome CDP:</b> chưa chạy; bấm nút Khởi động / kiểm tra Chrome."
        )

    def _update_screen(raw: str | None = None):
        target = None
        if raw:
            try:
                target = _screen_url(raw)
            except ValueError:
                screen_link.value = (
                    "<p style='color:#b3261e'><b>URL Kaggle không hợp lệ.</b> "
                    "Dùng URL hiện tại từ Run → Kaggle Jupyter Server → Copy VS Code Compatible URL.</p>"
                )
                return
        else:
            target = _discover_screen_url()
        if target:
            from html import escape
            safe = escape(target, quote=True)
            screen_link.value = (
                "<a href='" + safe + "' target='_blank' rel='noopener noreferrer' "
                "style='display:block;width:100%;box-sizing:border-box;text-align:center;"
                "padding:14px;background:#202124;color:#fff;font-weight:600;"
                "text-decoration:none;border-radius:24px;margin:8px 0'>"
                "Mở màn hình Chrome ↗</a>"
                "<p>Đường dẫn là của phiên Kaggle hiện tại. Không chia sẻ URL có token.</p>"
            )
            screen_url_input.layout.display = "none"
            screen_status.value = "<b>Màn hình:</b> đã tạo liên kết noVNC; cần bấm liên kết để xác minh trên điện thoại."
        else:
            screen_url_input.layout.display = "block"
            screen_link.value = (
                "<p>Không tự phát hiện được URL có xác thực. Trong menu Run, chọn "
                "<b>Kaggle Jupyter Server → Copy VS Code Compatible URL</b>; "
                "dán vào ô URL bên trên. URL được dùng trong RAM và ô sẽ được xoá sau khi tạo liên kết.</p>"
            )
            screen_status.value = "<b>Màn hình:</b> đang chờ URL phiên Kaggle."

    def _run_command(button, status_widget, script_name: str, timeout_seconds: int):
        if running["worker"]:
            return
        running["worker"] = True
        button.disabled = True
        status_widget.value = "<b>Đang chạy:</b> " + script_name
        _refresh_buttons()
        env = os.environ.copy()
        env["DISPLAY"] = ":99"
        env["TOOL_SYSTEM_CDP_URL"] = CDP_URL
        env["PYTHONPATH"] = str(ROOT / "src")
        try:
            with output:
                print("\n=== " + script_name + " ===", flush=True)
                proc = subprocess.Popen(
                    [sys.executable, "-u", str(ROOT / "scripts" / script_name)],
                    cwd=str(ROOT),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                started = time.monotonic()
                seen = []
                # Workers print machine-readable status lines with flush=True.
                # Print only those safe lines; avoid leaking source URLs, tokens or provider errors.
                while True:
                    line = proc.stdout.readline()
                    if line:
                        clean = line.rstrip("\r\n")
                        if clean.startswith("TOOL_SYSTEM_") or clean.startswith("NOVNC_") or clean.startswith("MISSING_PACKAGE:"):
                            print(clean, flush=True)
                            seen.append(clean)
                            seen = seen[-80:]
                    elif proc.poll() is not None:
                        break
                    if time.monotonic() - started > timeout_seconds:
                        proc.terminate()
                        try:
                            proc.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait(timeout=5)
                        raise TimeoutError("TOOL_SYSTEM_WORKER_TIMEOUT")
                code = proc.wait(timeout=10)
                if code == 0:
                    status_widget.value = "<b>Đã kết thúc tiến trình:</b> " + script_name + " (exit 0). Xem các dấu xác minh bên dưới."
                    print("WORKER_EXIT_CODE=0", flush=True)
                else:
                    status_widget.value = "<b>Chưa hoàn tất:</b> " + script_name + " (exit " + str(code) + ")."
                    print("WORKER_EXIT_CODE=" + str(code), flush=True)
        except Exception as exc:
            status_widget.value = "<b>Lỗi:</b> " + type(exc).__name__
            with output:
                print("TOOL_SYSTEM_DASHBOARD_ERROR=" + type(exc).__name__, flush=True)
        finally:
            running["worker"] = False
            _refresh_buttons()

    def _save_config(_):
        url = supabase_url.value.strip().rstrip("/")
        key = supabase_key.value.strip()
        token = telegram_token.value.strip()
        chat = telegram_chat.value.strip()
        if not url.startswith("https://") or not url.endswith(".supabase.co") or not key:
            config_status.value = (
                "<b>Cấu hình chưa hợp lệ.</b> Nhập Supabase URL và service key của project RIÊNG "
                "cho Tool System V2; không dùng key của Money AI cũ."
            )
            return
        os.environ["TOOL_SYSTEM_SUPABASE_URL"] = url
        os.environ["TOOL_SYSTEM_SUPABASE_SERVICE_ROLE_KEY"] = key
        if token:
            os.environ["TOOL_SYSTEM_TELEGRAM_BOT_TOKEN"] = token
        if chat:
            os.environ["TOOL_SYSTEM_TELEGRAM_OWNER_CHAT_ID"] = chat
        supabase_key.value = ""
        telegram_token.value = ""
        config_status.value = (
            "<b>Cấu hình đã nạp vào RAM phiên này.</b> Chưa chứng minh kết nối DB; "
            "không ghi khóa vào GitHub hay file. Sau khi Kaggle kernel reset, cần nạp lại hoặc tạo Kaggle Secrets cùng tên."
        )
        _refresh_buttons()

    def _start_chrome(_):
        if running["worker"]:
            return
        running["worker"] = True
        chrome_button.disabled = True
        with output:
            print("\n=== Chrome / display / noVNC ===", flush=True)
            try:
                proc = subprocess.run(
                    [sys.executable, "-u", str(ROOT / "scripts" / "restore_one_chrome.py")],
                    cwd=str(ROOT),
                    env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                    capture_output=True,
                    text=True,
                    timeout=900,
                    check=False,
                )
                for line in (proc.stdout + "\n" + proc.stderr).splitlines():
                    if line.startswith("TOOL_SYSTEM_") or line.startswith("NOVNC_") or line.startswith("MISSING_PACKAGE:"):
                        print(line[:260], flush=True)
                if proc.returncode == 0 and _port_open(9222):
                    chrome_status.value = "<b>Chrome CDP:</b> PASS."
                    print("TOOL_SYSTEM_CHROME_BUTTON=PASS", flush=True)
                else:
                    chrome_status.value = "<b>Chrome CDP:</b> chưa xác minh; xem mã lỗi trên."
                    print("TOOL_SYSTEM_CHROME_BUTTON=FAIL_EXIT_" + str(proc.returncode), flush=True)
            except Exception as exc:
                chrome_status.value = "<b>Chrome CDP:</b> lỗi " + type(exc).__name__
                print("TOOL_SYSTEM_CHROME_BUTTON=FAIL_" + type(exc).__name__, flush=True)
            finally:
                running["worker"] = False
                chrome_button.disabled = False
                _refresh_buttons()
                _update_screen()

    def _backup_profile(_):
        # Explicit click is the owner approval; this can briefly stop/restart Chrome.
        _run_command(backup_button, chrome_status, "save_chrome_profile.py", 600)

    def _screen_changed(change):
        raw = str(change.get("new") or "").strip()
        if not raw:
            return
        _update_screen(raw)
        screen_url_input.value = ""

    def _step05(_):
        _run_command(step05_button, step05_status, "run_business_step05.py", 800)

    def _step06(_):
        _run_command(step06_button, step06_status, "run_business_step06.py", 1500)

    def _step07(_):
        _run_command(step07_button, step07_status, "run_business_step07.py", 1500)

    save_config_button.on_click(_save_config)
    chrome_button.on_click(_start_chrome)
    backup_button.on_click(_backup_profile)
    step05_button.on_click(_step05)
    step06_button.on_click(_step06)
    step07_button.on_click(_step07)
    screen_url_input.observe(_screen_changed, names="value")

    _load_existing_config()
    _refresh_buttons()
    _update_screen()
    display(widgets.VBox([
        widgets.HTML("<h3>Tool System V2 — Hạ tầng Kaggle</h3><p>Nút khởi động Chrome/noVNC và nghiệp vụ bước 05–07 được tách riêng. Không chạy tự động khi mở dashboard.</p>"),
        chrome_button,
        chrome_status,
        widgets.HTML("<b>Màn hình Chrome</b>"),
        screen_status,
        screen_url_input,
        screen_link,
        widgets.HTML("<hr><h3>Cấu hình riêng của Tool System V2</h3><p>Không dùng khóa hoặc URL Supabase của Money AI cũ. Các giá trị sau chỉ được giữ trong RAM phiên Kaggle.</p>"),
        supabase_url,
        supabase_key,
        telegram_token,
        telegram_chat,
        save_config_button,
        config_status,
        backup_button,
        widgets.HTML("<hr><h3>Nghiệp vụ</h3><p>Bước 06 chỉ được chạy sau khi Bước 05 xác minh nguồn; Bước 07 chỉ sau khi Bước 06 xác minh kịch bản. Các worker giữ các cổng kiểm tra của Money AI.</p>"),
        step05_button, step05_status,
        step06_button, step06_status,
        step07_button, step07_status,
        output,
    ]))
