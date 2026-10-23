"""Consented brief stop/snapshot/restart of the EXISTING CDP 9222 Chrome.

Run from the new owner-approved cold-backup control in Kaggle Notebook.
NoVNC and Kaggle stay running. Credentials are AES-GCM encrypted before upload to
private money-ai-state bucket. An SHA-verified commit is printed, not cookies.
"""
from __future__ import annotations

import fcntl
import os
import json
import signal
import time
from contextlib import contextmanager
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))



@contextmanager
def _temporarily_close_existing_chrome(launcher: dict, session_path: Path,
                                       profile: Path, pid: int, argv: list[str]):
    """Quiesce only the validated CDP browser; always resume after ZIP.

    This briefly closes the owner's Chrome; Xvfb/noVNC and the notebook remain
    untouched. This is called only after a click on the explicit cold-backup
    button. No SIGKILL, no profile deletion, no extra Chrome ever.
    """
    chrome_pids = launcher["_chromium_processes"]()
    if pid not in chrome_pids or not launcher["_cdp_alive"]():
        raise RuntimeError("CHROME_COLD_BACKUP_BROWSER_NOT_RUNNING")
    try:
        if os.getpgid(pid) != pid or any(os.getpgid(child) != pid for child in chrome_pids):
            raise RuntimeError("CHROME_COLD_BACKUP_UNRELATED_BROWSER_PROCESS")
    except ProcessLookupError:
        raise RuntimeError("CHROME_COLD_BACKUP_PROCESS_CHANGED") from None
    if not argv or not Path(argv[0]).is_file():
        raise RuntimeError("CHROME_COLD_BACKUP_EXECUTABLE_NOT_FOUND")
    # Only the original browser group is involved. The subprocess spawned by
    # Tool System V2 has its own process session, separate from Xvfb/VNC/Jupyter.
    print("TOOL_SYSTEM_VAULT_STAGE=CHROME_CLOSE_START", flush=True)
    os.kill(pid, signal.SIGTERM)
    stopped = False
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if not launcher["_chromium_processes"]() and not launcher["_port_open"](9222):
                stopped = True
                break
            time.sleep(0.25)
        if not stopped:
            raise RuntimeError("CHROME_COLD_BACKUP_GRACEFUL_STOP_TIMEOUT")
        print("TOOL_SYSTEM_VAULT_STAGE=CHROME_CLOSED_FOR_SNAPSHOT", flush=True)
        yield
    finally:
        # Restart before any cloud upload, even when SQLite/ZIP fails. Never
        # start a second browser when a browser or port 9222 still exists.
        if launcher["_cdp_alive"]():
            print("TOOL_SYSTEM_VAULT_STAGE=CHROME_ALREADY_RESUMED", flush=True)
        elif launcher["_chromium_processes"]() or launcher["_port_open"](9222):
            print("TOOL_SYSTEM_VAULT_STAGE=CHROME_RESUME_BLOCKED_EXISTING_PROCESS", flush=True)
            raise RuntimeError("CHROME_COLD_BACKUP_CANNOT_RESTART_UNTIL_OLD_PROCESS_EXITS")
        else:
            print("TOOL_SYSTEM_VAULT_STAGE=CHROME_RESTART_START", flush=True)
            logs = launcher["WORKDIR"] / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            env = launcher["_safe_child_env"]()
            env["DISPLAY"] = ":99"
            process = launcher["_spawn"](argv, logs / "chrome.log", env=env)
            launcher["_await_browser_cdp"](process, seconds=40)
            restored = {"profile": str(profile), "chrome_pid": process.pid}
            tmp = session_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(restored), encoding="utf-8")
            os.replace(tmp, session_path)
            print("TOOL_SYSTEM_VAULT_STAGE=CHROME_RESTART_PASS", flush=True)

def save() -> str:
    print("TOOL_SYSTEM_VAULT_STAGE=CHILD_STARTED", flush=True)
    if not Path("/kaggle").is_dir():
        raise RuntimeError("CHROME_VAULT_KAGGLE_ONLY")
    if not os.environ.get("TOOL_SYSTEM_SUPABASE_SERVICE_ROLE_KEY"):
        raise RuntimeError("CHROME_VAULT_SUPABASE_ACTIVATION_REQUIRED")
    # Do not let an old Notebook widget unexpectedly stop the signed-in
    # browser after an automatic code update. Only the NEW explicitly labeled
    # cold-backup button may opt in to this short Chrome restart.
    if os.environ.get("TOOL_SYSTEM_CHROME_COLD_BACKUP_CONSENT") != "1":
        raise RuntimeError("CHROME_COLD_BACKUP_OWNER_APPROVAL_REQUIRED")
    # Inspection only: the launcher functions below do NOT call start().
    print("TOOL_SYSTEM_VAULT_STAGE=VERIFY_CDP", flush=True)
    launcher = runpy.run_path(str(ROOT / "scripts" / "kaggle_cpu_desktop.py"))
    if not launcher["_cdp_alive"]():
        raise RuntimeError("CHROME_VAULT_SINGLE_CDP_9222_NOT_RUNNING")
    # Read the user-data-dir recorded when the ACTUAL shared CDP Chrome started.
    # Recomputing _profile_dir() after startup could select an unrelated stale
    # /kaggle/working/tool-system-v2-runtime/chrome_profile and produce a false empty-profile
    # error, even while the owner's signed-in Chrome is running successfully.
    print("TOOL_SYSTEM_VAULT_STAGE=VERIFY_SESSION_PROFILE", flush=True)
    import json
    meta_path = launcher["WORKDIR"] / "session.json"
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        profile = Path(metadata["profile"]).resolve(strict=True)
        pid = int(metadata["chrome_pid"])
        argv = [part.decode("utf-8", errors="surrogateescape")
                for part in Path(f"/proc/{pid}/cmdline").read_bytes().split(bytes([0])) if part]
        flags = [part.encode("utf-8", errors="surrogateescape") for part in argv]
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError("CHROME_VAULT_ACTIVE_SESSION_METADATA_INVALID") from None
    authorized = {
        Path("/kaggle/working/tool-system-v2-runtime/chrome_profile").resolve(),
        (launcher["WORKDIR"] / "chrome_profile").resolve(),
    }
    if profile not in authorized:
        raise RuntimeError("CHROME_VAULT_PROFILE_PATH_NOT_AUTHORIZED")
    if (f"--user-data-dir={profile}".encode() not in flags
            or b"--remote-debugging-port=9222" not in flags):
        raise RuntimeError("CHROME_VAULT_PROFILE_NOT_THE_RUNNING_CDP_CHROME")
    print("TOOL_SYSTEM_VAULT_STAGE=LOAD_ENCRYPTION", flush=True)
    from money_ai.chrome_vault import ChromeVault, profile_initialized
    if not profile_initialized(profile):
        print("TOOL_SYSTEM_CHROME_PROFILE_DIAGNOSTIC: " +
              "local_state=" + str((profile / "Local State").is_file()) +
              " default=" + str((profile / "Default").is_dir()) +
              " numbered_profiles=" + str(sum(
                  p.is_dir() and p.name.startswith("Profile ")
                  for p in profile.iterdir())), flush=True)
        raise RuntimeError("CHROME_VAULT_ACTIVE_PROFILE_NOT_INITIALIZED")
    print("TOOL_SYSTEM_VAULT_STAGE=LOCK_BACKUP", flush=True)
    lock = Path("/kaggle/working/tool-system-v2-runtime/state/chrome-vault.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as holder:
        try:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("CHROME_VAULT_BACKUP_ALREADY_RUNNING") from None
        print("TOOL_SYSTEM_VAULT_STAGE=BACKUP_BEGIN", flush=True)
        print("TOOL_SYSTEM_VAULT_STAGE=COLD_BACKUP_MODE", flush=True)
        digest = ChromeVault().backup(
            profile, snapshot_context=lambda: _temporarily_close_existing_chrome(
                launcher, meta_path, profile, pid, argv))
    # SHA check proves upload/readback, NOT Facebook/Google login validity.
    print("TOOL_SYSTEM_CHROME_PROFILE_CLOUD_BACKUP=PASS", flush=True)
    print("TOOL_SYSTEM_CHROME_PROFILE_SHA256=" + digest[:16], flush=True)
    print("CHROME_CDP=REUSED_9222; AUTH_STATUS=NOT_YET_VERIFIED", flush=True)
    return digest


if __name__ == "__main__":
    try:
        save()
    except Exception as exc:
        print("TOOL_SYSTEM_CHROME_PROFILE_CLOUD_BACKUP=FAIL:" +
              type(exc).__name__ + ":" + str(exc)[:110], flush=True)
        raise
