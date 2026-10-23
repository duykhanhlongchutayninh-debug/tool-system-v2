"""Kaggle owner setup: verify new Supabase credentials without Kaggle Secrets.

Run this module ONLY from the current Kaggle Notebook after the code-sync
watcher is live. Owner enters a new project's server-side key in a masked UI.
No credentials are stored in a notebook code cell, GitHub, logs, or disk.

The validation initializes the real SQLite Store only if not yet present,
uploads a consistent immutable SQLite snapshot to Supabase private Storage,
verifies readback SHA-256 and restores it to a temporary path for comparison.
The Supabase verification itself never touches Chrome. The separate opt-in
Chrome backup button captures the existing CDP 9222 profile after owner login.
"""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import tempfile

from .cloud_checkpoint import CloudCheckpoint
from .durable_store import DurableStore

DEFAULT_STATE = Path("/kaggle/working/tool-system-v2-runtime/state/jobs.sqlite")


class SupabaseActivationError(RuntimeError):
    pass


def validate_key(key: str) -> str:
    key = key.strip()
    # Modern secret keys, or legacy service-role signed JWT (not anon keys).
    if key.startswith("sb_secret_") and len(key) > 25:
        return key
    if key.startswith("eyJ") and key.count(".") == 2 and len(key) > 60:
        return key
    raise SupabaseActivationError(
        "INVALID_KEY_FORMAT: use the new Tool System V2 project's Secret key "
        "(sb_secret_...) or its legacy service_role JWT, not publishable/anon."
    )


def verify_connection(secret: str, state_path: str | Path = DEFAULT_STATE) -> dict:
    """Verify the *real* project, private bucket and checkpoint round trip.

    A successful validation retains the secret only in the active Notebook
    process environment for later server-side Tool System V2 operations.
    A failure does not establish a production runtime.
    """
    secret = validate_key(secret)
    checkpoint = CloudCheckpoint(service_key=secret)
    # Read from the new money-ai project before writing anything.
    checkpoint.latest()

    path = Path(state_path)
    store = DurableStore(path, checkpoint)
    try:
        # This will be the actual project Store, not fake fixture data.
        current_jobs = len(store.list_jobs())
    finally:
        store.close()

    digest = checkpoint.backup(path)
    with tempfile.TemporaryDirectory(prefix="money-ai-checkpoint-verify-") as directory:
        target = Path(directory) / "restored.sqlite"
        restored = checkpoint.restore(target)
        if not restored:
            raise SupabaseActivationError("CLOUD_CHECKPOINT_NOT_FOUND_AFTER_UPLOAD")
        with sqlite3.connect(str(target)) as database:
            jobs_after = database.execute("select count(*) from jobs").fetchone()[0]
        if jobs_after != current_jobs:
            raise SupabaseActivationError("CLOUD_RESTORE_JOBS_COUNT_MISMATCH")
    # Expose only to this live Kaggle Python process; DO NOT write it to disk
    # or send to stdout. Not persisted after Kaggle session ends.
    os.environ["TOOL_SYSTEM_SUPABASE_SERVICE_ROLE_KEY"] = secret
    os.environ["TOOL_SYSTEM_SUPABASE_URL"] = checkpoint.url
    return {"project": "money-ai", "schema": "SQLite jobs/events/lessons",
            "jobs": current_jobs, "snapshot_sha": digest[:12],
            "result": "TOOL_SYSTEM_SUPABASE_BACKUP_RESTORE_PASS"}


def show_activation_ui() -> None:
    """Small Android-friendly masked key input and one real verification button."""
    import ipywidgets as widgets
    from IPython.display import display, clear_output

    password = widgets.Password(
        description="Supabase:",
        placeholder="Dán Secret key của project money-ai",
        layout=widgets.Layout(width="95%")
    )
    button = widgets.Button(
        description="Kết nối và kiểm tra Supabase", button_style="success",
        layout=widgets.Layout(width="95%")
    )
    save_chrome = widgets.Button(
        description="Sao lưu Chrome hiện tại lên Supabase",
        button_style="info",
        disabled=not bool(os.environ.get("TOOL_SYSTEM_SUPABASE_SERVICE_ROLE_KEY")),
        layout=widgets.Layout(width="95%")
    )
    output = widgets.Output(layout=widgets.Layout(width="95%"))

    def on_click(_):
        if button.disabled:
            return
        button.disabled = True
        with output:
            clear_output(wait=True)
            print("Đang kiểm tra Supabase mới và sao lưu thực tế...")
        try:
            status = verify_connection(password.value)
        except Exception as exc:
            with output:
                print(f"SUPABASE_ACTIVATION_FAILED: {type(exc).__name__}: {str(exc)[:180]}")
            button.disabled = False
        else:
            with output:
                print(status["result"])
                print(f"Project: {status['project']}; saved jobs: {status['jobs']}; "
                      f"verified snapshot SHA: {status['snapshot_sha']}")
                print("Kết nối chỉ hoạt động trong phiên Kaggle hiện tại.")
            save_chrome.disabled = False
        finally:
            password.value = ""

    def on_save_chrome(_):
        if save_chrome.disabled:
            return
        save_chrome.disabled = True
        with output:
            clear_output(wait=True)
            print("Đang mã hóa và xác minh sao lưu hồ sơ Chrome hiện hữu...")
        try:
            import runpy
            entry = Path(__file__).resolve().parents[2] / "scripts" / "save_chrome_profile.py"
            runpy.run_path(str(entry), run_name="__main__")
            with output:
                print("Sao lưu dữ liệu Chrome hoàn tất; cần xác minh trạng thái đăng nhập trực tiếp.")
        except Exception as exc:
            with output:
                print("CHROME_VAULT_BACKUP_FAILED:" + type(exc).__name__ +
                      ":" + str(exc)[:110])
        finally:
            save_chrome.disabled = False

    button.on_click(on_click)
    save_chrome.on_click(on_save_chrome)
    display(widgets.VBox([password, button, save_chrome, output]))
