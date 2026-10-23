"""One-screen, one-Chrome Kaggle CPU launcher; no public tunnel or extra host.

Run inside the *existing* Kaggle CPU notebook, not on GitHub Actions.
No user credentials are accepted here. Mobile display requires a working,
authenticated Jupyter server proxy; this script NEVER asserts it is reachable.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

WORKDIR = Path(os.environ.get("TOOL_SYSTEM_HOME", "/kaggle/working/tool-system-v2-runtime"))
DISPLAY = ":99"
CDP = "http://127.0.0.1:9222"
SCREEN = "http://127.0.0.1:6080/vnc.html"

# Kaggle's authenticated Jupyter proxy already ends in "/proxy". To expose a
# kernel port, its second proxy route must be appended as "/proxy/<port>/...".
# Therefore the full noVNC URL ends in "/proxy/proxy/6080/vnc.html".
SCREEN_PROXY_SUFFIX = "/proxy/6080/vnc.html"
KAGGLE_PROXY_HOST = "kkb-production.jupyter-proxy.kaggle.net"


def kaggle_screen_url(proxy_base: str) -> str:
    """Build the noVNC URL from the copied VS Code-compatible Kaggle proxy URL.

    The base is the authenticated URL ending in /k/<id>/<session-token>/proxy.
    This function never fetches, prints, or stores the session token.
    """
    if not isinstance(proxy_base, str) or not proxy_base.strip():
        raise ValueError("KAGGLE_PROXY_BASE_REQUIRED")
    parsed = urlsplit(proxy_base.strip())
    if parsed.scheme != "https" or parsed.hostname != KAGGLE_PROXY_HOST:
        raise ValueError("KAGGLE_PROXY_BASE_INVALID_HOST")
    base_path = parsed.path.rstrip("/")
    pieces = base_path.split("/")
    if len(pieces) < 5 or pieces[-1] != "proxy" or pieces[1] != "k":
        raise ValueError("KAGGLE_PROXY_BASE_MUST_END_IN_AUTHENTICATED_PROXY")
    # Do not carry query/fragment data into the VNC path.
    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        base_path + SCREEN_PROXY_SUFFIX,
        "",
        "",
    ))


def _binary(*names: str) -> str:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError(f"MISSING_PACKAGE: {names[0]} (install on Kaggle CPU; do not change architecture)")

def _safe_child_env() -> dict[str, str]:
    """Exclude all credentials when launching Chrome and test helper processes.

    SERVICE_ROLE_KEY (legacy Supabase admin key) has no TOKEN/SECRET substring;
    filtering only those would silently leak it. Keep DISPLAY, PATH and
    PLAYWRIGHT_BROWSERS_PATH needed for a running GUI.
    """
    sensitive = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL",
                 "AUTH", "COOKIE", "PRIVATE", "SESSION")
    return {name: value for name, value in os.environ.items()
            if not any(term in name.upper() for term in sensitive)}


def _usable_browser(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _playwright_chrome_path() -> Path | None:
    """Ask installed Playwright to resolve its own Chromium path.

    Inspecting BrowserType.executable_path does NOT launch a browser. Use a
    short-lived subprocess because Jupyter already runs an asyncio loop and
    Playwright's synchronous API must not execute directly in that loop.
    """
    code = """from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    print(p.chromium.executable_path)
"""
    # Do not give the helper our GitHub / Supabase / Telegram credentials.
    env = _safe_child_env()
    try:
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            env=env, timeout=20, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode or not result.stdout.strip():
        return None
    return Path(result.stdout.strip().splitlines()[-1])


def _discover_chrome_binary() -> str | None:
    """Find only a headed/full Chromium, never a headless-only shell."""
    override = os.environ.get("CHROMIUM_PATH", "").strip()
    if override:
        path = Path(override)
        if not _usable_browser(path):
            raise RuntimeError("CHROMIUM_PATH_INVALID")
        return str(path)
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        found = shutil.which(name)
        if found and _usable_browser(Path(found)):
            return found
    resolved = _playwright_chrome_path()
    if resolved and _usable_browser(resolved):
        return str(resolved)
    roots = []
    custom = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if custom and custom != "0":
        roots.append(Path(custom))
    roots.extend((WORKDIR / "playwright-browsers",
                  Path.home() / ".cache" / "ms-playwright",
                  Path("/root/.cache/ms-playwright"),
                  Path("/kaggle/working/.cache/ms-playwright")))
    patterns = ("chromium-*/chrome-linux64/chrome",
                "chromium-*/chrome-linux/chrome")
    for root in roots:
        for pattern in patterns:
            for path in sorted(root.glob(pattern), reverse=True):
                if _usable_browser(path):
                    return str(path)
    return None


def _chrome_binary() -> str:
    """Reuse installed Chromium; download FULL browser once only if missing."""
    found = _discover_chrome_binary()
    if found:
        return found
    # Existing Kaggle Chrome process is checked BEFORE this function runs.
    # A kernel/session reset can delete Playwright browser cache while keeping
    # the Python package; restore only the required browser, not Chromium deps,
    # Xvfb, VNC, profile, GitHub updater, or Supabase setup.
    print("TOOL_SYSTEM_CHROMIUM_MISSING: installing full Chromium once", flush=True)
    # Prefer /kaggle/working for repeated kernel runs instead of ephemeral
    # /root/.cache. This is not guaranteed durable across Kaggle sessions.
    browser_dir = WORKDIR / "playwright-browsers"
    browser_dir.mkdir(parents=True, exist_ok=True)
    install_env = _safe_child_env()
    install_env["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_dir)
    log = WORKDIR / "logs" / "chrome_install.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as output:
        try:
            result = subprocess.run(
                [sys.executable, "-m", "playwright", "install",
                 "--no-shell", "chromium"],
                stdout=output, stderr=subprocess.STDOUT, env=install_env,
                stdin=subprocess.DEVNULL, timeout=360, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError(
                "CHROMIUM_INSTALL_TIMEOUT_OR_OS_ERROR: check logs/chrome_install.log"
            ) from None
    if result.returncode:
        raise RuntimeError(
            f"CHROMIUM_INSTALL_FAILED_EXIT_{result.returncode}: "
            "check logs/chrome_install.log"
        )
    found = _discover_chrome_binary()
    if not found:
        raise RuntimeError(
            "CHROMIUM_INSTALLED_BUT_NOT_FOUND: check logs/chrome_install.log"
        )
    print("TOOL_SYSTEM_CHROMIUM_READY", flush=True)
    return found



def _missing_chrome_libraries(chrome: str) -> list[str]:
    """Read native dynamic-linker requirements without starting Chromium."""
    try:
        checked = subprocess.run(
            ["ldd", chrome], capture_output=True, text=True,
            env=_safe_child_env(), timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("CHROMIUM_LDD_CHECK_FAILED") from None
    output = (checked.stdout or "") + "\n" + (checked.stderr or "")
    if checked.returncode != 0:
        raise RuntimeError("CHROMIUM_LDD_CHECK_FAILED: check binary dependencies")
    missing = []
    for line in output.splitlines():
        parts = line.strip().split()
        if len(parts) >= 4 and parts[1:4] == ["=>", "not", "found"]:
            missing.append(parts[0])
    return sorted(set(missing))


def _ensure_chromium_dependencies(chrome: str) -> None:
    """Only when ldd finds missing .so, install official Playwright OS deps.

    This is the exact browser dependency set, not another Chrome download.
    Only one attempted install per browser+missing-libraries fingerprint.
    Success is reported only if a second ldd confirms no missing libraries.
    """
    missing = _missing_chrome_libraries(chrome)
    if not missing:
        return
    print("TOOL_SYSTEM_CHROME_MISSING_LIBRARIES:", ", ".join(missing), flush=True)
    if os.geteuid() != 0:
        raise RuntimeError("CHROMIUM_OS_DEPS_NEED_ROOT: cannot install in this Kaggle session")
    # Persist a failure sentinel in Kaggle workdir so failed package commands
    # are never retried blindly on every notebook run.
    stamp = WORKDIR / "logs" / "chrome_deps_failed.txt"
    signature = "|".join(missing)
    if stamp.exists() and stamp.read_text(encoding="utf-8") == signature:
        raise RuntimeError("CHROMIUM_OS_DEPS_PREVIOUS_ATTEMPT_FAILED: inspect chrome_deps.log")
    log = WORKDIR / "logs" / "chrome_deps.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    print("TOOL_SYSTEM_CHROME_INSTALLING_SYSTEM_DEPS", flush=True)
    with log.open("ab") as output:
        try:
            result = subprocess.run(
                [sys.executable, "-m", "playwright", "install-deps", "chromium"],
                stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env=_safe_child_env(), timeout=420, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            stamp.write_text(signature, encoding="utf-8")
            raise RuntimeError(
                "CHROMIUM_OS_DEPS_INSTALL_TIMEOUT: inspect chrome_deps.log"
            ) from None
    if result.returncode:
        stamp.write_text(signature, encoding="utf-8")
        raise RuntimeError(
            "CHROMIUM_OS_DEPS_INSTALL_FAILED: inspect chrome_deps.log"
        )
    remaining = _missing_chrome_libraries(chrome)
    if remaining:
        stamp.write_text("|".join(remaining), encoding="utf-8")
        raise RuntimeError(
            "CHROMIUM_OS_DEPS_STILL_MISSING: " + ", ".join(remaining)
        )
    stamp.unlink(missing_ok=True)
    print("TOOL_SYSTEM_CHROME_SYSTEM_DEPS_READY", flush=True)


def _profile_dir() -> Path:
    """Prioritize the original owner's actual Playwright profile; do not switch."""
    previous = Path("/kaggle/working/tool-system-v2-runtime/chrome_profile")
    if previous.is_dir():
        return previous
    return WORKDIR / "chrome_profile"


def _chromium_processes() -> list[int]:
    """Detect a browser that exists without CDP; never start a duplicate."""
    found = []
    proc = Path("/proc")
    for item in proc.iterdir():
        if not item.name.isdigit():
            continue
        try:
            exe = (item / "exe").resolve(strict=True).name.lower()
            state = (item / "stat").read_text().split(") ", 1)[1][:1]
        except (OSError, ValueError, IndexError):
            continue
        if state == "Z":
            continue
        if exe in ("chrome", "chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
            found.append(int(item.name))
    return found


def _process_command_line(pid: int) -> list[str]:
    """Read one live process's argv without shelling out or logging arguments."""
    if not isinstance(pid, int) or pid <= 1:
        return []
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", errors="replace")
            for part in raw.split(bytes([0])) if part]


def _verified_running_chrome_profile(session_file: Path) -> tuple[Path, int]:
    """Prove the existing CDP owner and return the profile it actually uses.

    session.json records which process Tool System V2 started. A preferred profile
    directory can change after a restore; that must not make us start a second
    Chrome or discard the current login. Trust only the recorded PID when it
    is still a live Chromium process whose argv explicitly owns CDP 9222 and
    names an existing user-data-dir.
    """
    try:
        session = json.loads(session_file.read_text(encoding="utf-8"))
        pid = int(session.get("chrome_pid", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raise RuntimeError("CHROME_SESSION_METADATA_INVALID") from None
    if pid not in _chromium_processes():
        raise RuntimeError("CHROME_SESSION_PID_NOT_RUNNING")
    args = _process_command_line(pid)
    if not args:
        raise RuntimeError("CHROME_SESSION_PROCESS_ARGS_UNAVAILABLE")
    if not any(arg == "--remote-debugging-port=9222" for arg in args):
        raise RuntimeError("CHROME_SESSION_PID_NOT_CDP_OWNER")
    user_data = None
    for index, arg in enumerate(args):
        if arg.startswith("--user-data-dir="):
            user_data = arg.split("=", 1)[1]
            break
        if arg == "--user-data-dir" and index + 1 < len(args):
            user_data = args[index + 1]
            break
    if not user_data:
        raise RuntimeError("CHROME_SESSION_PROFILE_ARGUMENT_MISSING")
    candidate = Path(user_data).expanduser()
    if not candidate.is_absolute() or not candidate.is_dir():
        raise RuntimeError("CHROME_SESSION_PROFILE_NOT_AVAILABLE")
    try:
        actual = candidate.resolve(strict=True)
    except OSError:
        raise RuntimeError("CHROME_SESSION_PROFILE_NOT_AVAILABLE") from None
    return actual, pid




def _display_alive(display: str) -> bool:
    """Probe actual X11 server with libX11, not optional xdpyinfo binary.

    Chromium requires libX11 anyway. A stale X socket alone is not evidence
    that the display can accept real browser clients.
    """
    try:
        xlib = ctypes.CDLL("libX11.so.6")
    except OSError:
        raise RuntimeError("DISPLAY_X11_LIBRARY_MISSING: Chromium desktop dependencies unavailable") from None
    open_display = xlib.XOpenDisplay
    open_display.argtypes = [ctypes.c_char_p]
    open_display.restype = ctypes.c_void_p
    close_display = xlib.XCloseDisplay
    close_display.argtypes = [ctypes.c_void_p]
    close_display.restype = ctypes.c_int
    handle = open_display(display.encode("ascii"))
    if not handle:
        return False
    close_display(handle)
    return True


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _cdp_alive() -> bool:
    try:
        with urlopen(CDP + "/json/version", timeout=3) as r:
            data = json.load(r)
        return bool(data.get("webSocketDebuggerUrl"))
    except (OSError, ValueError):
        return False


def _spawn(argv: list[str], log: Path, env=None) -> subprocess.Popen:
    stream = log.open("ab", buffering=0)
    try:
        p = subprocess.Popen(argv, stdout=stream, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True, env=env)
    finally:
        stream.close()
    return p


def _await(predicate, label: str, seconds: int = 15):
    for _ in range(seconds * 5):
        if predicate():
            return
        time.sleep(0.2)
    raise RuntimeError(f"{label}_NOT_READY; check logs in {WORKDIR / 'logs'}")



def _novnc_webroot() -> Path | None:
    """Resolve the HTML client installed by the OS's noVNC package."""
    return next(
        (p for p in (Path("/usr/share/novnc"), Path("/usr/local/share/novnc"))
         if (p / "vnc.html").is_file()), None
    )


def _missing_viewer_packages() -> list[str]:
    """Audit the whole VNC stack BEFORE trying to start any component."""
    missing: list[str] = []
    if shutil.which("x11vnc") is None:
        missing.append("x11vnc")
    if shutil.which("websockify") is None:
        missing.append("websockify")
    if _novnc_webroot() is None:
        missing.append("novnc")
    return missing


def _ensure_viewer_dependencies() -> None:
    """One bounded apt batch for ALL absent viewer components.

    Kaggle CPU sessions may discard apt-installed binaries. Restore only
    missing x11vnc/websockify/noVNC packages; never reinstall Chromium,
    restart Kaggle/Chrome, or change the owner's Chrome profile.
    """
    missing = _missing_viewer_packages()
    if not missing:
        print("TOOL_SYSTEM_VIEWER_DEPENDENCIES_READY", flush=True)
        return
    print("TOOL_SYSTEM_VIEWER_MISSING:", ", ".join(missing), flush=True)
    if os.geteuid() != 0:
        raise RuntimeError("VIEWER_PACKAGES_REQUIRE_ROOT: do not reinstall Chrome")
    apt = shutil.which("apt-get")
    if not apt:
        raise RuntimeError("VIEWER_APT_UNAVAILABLE: no apt-get in Kaggle")
    logs = WORKDIR / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    failed = logs / "viewer_deps_failed.txt"
    fingerprint = "|".join(missing)
    if failed.is_file() and failed.read_text(encoding="utf-8") == fingerprint:
        raise RuntimeError("VIEWER_PACKAGES_PREVIOUS_ATTEMPT_FAILED: inspect viewer_install.log")
    env = _safe_child_env()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    print("TOOL_SYSTEM_VIEWER_INSTALLING_DEPENDENCIES", flush=True)
    with (logs / "viewer_install.log").open("ab") as log:
        for cmd, timeout in (
            ([apt, "-qq", "update"], 240),
            ([apt, "-y", "-qq", "install", "--no-install-recommends", *missing], 360),
        ):
            try:
                result = subprocess.run(
                    cmd, stdout=log, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, env=env,
                    timeout=timeout, check=False
                )
            except (OSError, subprocess.TimeoutExpired):
                failed.write_text(fingerprint, encoding="utf-8")
                raise RuntimeError(
                    "VIEWER_PACKAGE_INSTALL_TIMEOUT: inspect viewer_install.log"
                ) from None
            if result.returncode != 0:
                failed.write_text(fingerprint, encoding="utf-8")
                raise RuntimeError(
                    "VIEWER_PACKAGE_INSTALL_FAILED: inspect viewer_install.log"
                )
    remaining = _missing_viewer_packages()
    if remaining:
        failed.write_text("|".join(remaining), encoding="utf-8")
        raise RuntimeError("VIEWER_PACKAGES_STILL_MISSING: " + ", ".join(remaining))
    failed.unlink(missing_ok=True)
    print("TOOL_SYSTEM_VIEWER_DEPENDENCIES_READY", flush=True)


def _patch_novnc_proxy() -> None:
    """Reapply the verified Kaggle WebSocket prefix patch after apt restore."""
    import runpy
    patch = Path(__file__).with_name("fix_novnc_kaggle_proxy.py")
    if not patch.is_file():
        raise RuntimeError("NOVNC_PROXY_PATCH_MISSING")
    runpy.run_path(str(patch), run_name="__main__")


def _novnc_http_alive() -> bool:
    """Verify real HTML is served, not merely that TCP 6080 accepts connections."""
    try:
        with urlopen(SCREEN, timeout=4) as response:
            sample = response.read(4096).lower()
            return response.status == 200 and (
                b"novnc" in sample or b"<html" in sample)
    except (OSError, ValueError):
        return False


def _await_browser_cdp(process: subprocess.Popen, seconds: int = 40) -> None:
    """Fail immediately if Chrome exits, not a blind 25-second CDP timeout."""
    for _ in range(seconds * 5):
        if _cdp_alive():
            return
        if process.poll() is not None:
            raise RuntimeError(
                "CHROME_PROCESS_EXITED_EARLY: check /kaggle/working/tool-system-v2-runtime/logs/chrome.log"
            )
        time.sleep(0.2)
    raise RuntimeError(
        "CHROME_CDP_NOT_READY: check /kaggle/working/tool-system-v2-runtime/logs/chrome.log"
    )


def start() -> dict:
    """Idempotent; never launches a second Chrome if another owns the CDP port."""
    if not Path('/kaggle').exists():
        raise RuntimeError("KAGGLE_ONLY: run this on the existing Kaggle CPU notebook")
    WORKDIR.mkdir(parents=True, exist_ok=True)
    os.chmod(WORKDIR, 0o700)
    logs = WORKDIR / "logs"
    logs.mkdir(exist_ok=True)
    profile = _profile_dir()
    profile_restore = "LOCAL_PROFILE_REUSED"
    # Restore only from the separately configured Tool System V2 Supabase project.
    # Never read Money AI's credentials or connect to its old project.
    if not _port_open(9222) and not _chromium_processes():
        from money_ai.chrome_vault import ChromeVault, profile_initialized
        if profile_initialized(profile):
            profile_restore = "LOCAL_PROFILE_REUSED"
        else:
            if profile.exists() and any(profile.iterdir()):
                raise RuntimeError("TOOL_SYSTEM_CHROME_PROFILE_INCOMPLETE: preserve profile; refusing overwrite")
            project_url = os.environ.get("TOOL_SYSTEM_SUPABASE_URL", "").strip()
            service_key = os.environ.get("TOOL_SYSTEM_SUPABASE_SERVICE_ROLE_KEY", "").strip()
            if project_url and service_key:
                restored = ChromeVault().restore(profile)
                profile_restore = "CLOUD_VERIFIED" if restored else "NO_CLOUD_BACKUP_YET"
            else:
                profile_restore = "FRESH_PROFILE_NO_CLOUD_RESTORE"
            print("TOOL_SYSTEM_CHROME_PROFILE_RESTORE=" + profile_restore, flush=True)
    meta = WORKDIR / "session.json"

    # The Jupyter kernel may have been restarted with DISPLAY set but Xvfb
    # stopped; check the actual socket rather than trusting an env variable.
    if not os.environ.get("DISPLAY"):
        os.environ["DISPLAY"] = DISPLAY
    if os.environ["DISPLAY"] != DISPLAY:
        raise RuntimeError("UNEXPECTED_DISPLAY: refuse to open another desktop")
    if not Path("/tmp/.X11-unix/X99").exists():
        xvfb = _binary("Xvfb")
        _spawn([xvfb, DISPLAY, "-screen", "0", "1366x900x24", "-nolisten", "tcp"],
               logs / "display.log")
        _await(lambda: Path("/tmp/.X11-unix/X99").exists(), "DISPLAY")
    # Probe the real X server directly; minimal Kaggle images may not include
    # the optional xdpyinfo CLI even when Xvfb and Chromium are working.
    if not _display_alive(os.environ["DISPLAY"]):
        raise RuntimeError("DISPLAY_NOT_WORKING: X11 server is not accepting clients")

    # One preflight for *all* Android viewer dependencies, before browser
    # launch. Missing components are restored together, not one error at a time.
    _ensure_viewer_dependencies()
    _patch_novnc_proxy()

    if _port_open(9222):
        if not _cdp_alive() or not meta.exists():
            raise RuntimeError("CDP_PORT_ALREADY_USED: refuse to attach to an unknown Chrome")
        # The live, recorded Chrome process is authoritative for its profile.
        # If the preferred path changed after restore, reconcile metadata to the
        # profile proved by argv; never reject the live browser or spawn another.
        active_profile, active_pid = _verified_running_chrome_profile(meta)
        if active_profile != profile.resolve():
            print("TOOL_SYSTEM_CHROME_PROFILE_REUSED_FROM_ACTIVE_CDP=PASS", flush=True)
            profile = active_profile
        else:
            print("TOOL_SYSTEM_CHROME_PROFILE_MATCH=PASS", flush=True)
        meta.write_text(json.dumps({
            "profile": str(profile), "chrome_pid": active_pid,
        }), encoding="utf-8")
        reused = True
    else:
        if _chromium_processes():
            raise RuntimeError("CHROME_ALREADY_RUNNING_WITHOUT_CDP: refuse second Chrome")
        profile.mkdir(parents=True, exist_ok=True)
        os.chmod(profile, 0o700)
        chrome = _chrome_binary()
        _ensure_chromium_dependencies(chrome)
        chrome_flags = [
            chrome, f"--user-data-dir={profile}", "--remote-debugging-port=9222",
            "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
            "--no-sandbox", "--disable-dev-shm-usage", "--window-size=1200,830", "about:blank",
        ]
        process = _spawn(chrome_flags, logs / "chrome.log", env=_safe_child_env())
        _await_browser_cdp(process, seconds=40)
        meta.write_text(json.dumps({"profile": str(profile), "chrome_pid": process.pid}))
        reused = False

    # Confirm the browser/CDP milestone separately. Viewer availability is a
    # different milestone and must not obscure an already-working Chrome.
    print("TOOL_SYSTEM_CHROME_CDP=PASS", flush=True)

    # Lightweight screen viewer for owner. Does not launch any other Chrome.
    vnc = _binary("x11vnc")
    websockify = _binary("websockify")
    webroot = _novnc_webroot()
    if webroot is None:
        raise RuntimeError("NOVNC_FILES_MISSING_AFTER_PREFLIGHT")

    # Owner-approved minimal setup: no separate VNC password prompt.
    # Keep RFB on loopback; authenticated Kaggle gateway is the external boundary.
    # Never bind the unauthenticated VNC port to a public network interface.
    if not _port_open(5900):
        _spawn([vnc, "-display", os.environ["DISPLAY"], "-rfbport", "5900",
                "-nopw", "-localhost", "-forever", "-shared", "-noxdamage"],
               logs / "vnc.log")
        _await(lambda: _port_open(5900), "VNC")
    if not _port_open(6080):
        _spawn([websockify, "--web", str(webroot), "127.0.0.1:6080", "127.0.0.1:5900"],
               logs / "screen.log")
        _await(lambda: _port_open(6080), "NOVNC")
    _await(_novnc_http_alive, "NOVNC_HTTP", seconds=12)

    result = {"chrome": "CDP_OK", "reused": reused, "cdp": CDP,
              "display": os.environ["DISPLAY"], "screen_local": SCREEN,
              "screen_proxy_suffix": SCREEN_PROXY_SUFFIX,
              "remote_mobile": "NOT_VERIFIED", "profile": str(profile),
              "profile_restore": profile_restore}
    print("TOOL_SYSTEM_SCREEN_LOCAL=PASS")
    print("TOOL_SYSTEM_MOBILE_SCREEN=NOT_VERIFIED: must open the authenticated Kaggle port route")
    print("TOOL_SYSTEM_SCREEN_PROXY_SUFFIX=" + SCREEN_PROXY_SUFFIX)
    print("TOOL_SYSTEM_SCREEN_URL_TEMPLATE=https://kkb-production.jupyter-proxy.kaggle.net/k/<KERNEL_ID>/<SESSION_TOKEN>/proxy/proxy/6080/vnc.html")
    print("TOOL_SYSTEM_SCREEN_URL_NOTE=Append SCREEN_PROXY_SUFFIX to the copied VS Code URL ending in /proxy; do not append only /6080/vnc.html")
    print("VNC_PASSWORD_PROMPT=DISABLED (loopback only)")
    return result


if __name__ == "__main__":
    start()
