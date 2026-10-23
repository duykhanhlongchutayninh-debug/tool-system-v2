"""CPU-only Chrome smoke test; does NOT sign in or connect to providers."""
from pathlib import Path
import os
import shutil
from playwright.sync_api import sync_playwright

root = Path(os.getenv("TOOL_SYSTEM_WORKDIR", "/kaggle/working/tool-system-v2-runtime"))
if not Path("/kaggle").exists():
    root = Path("./state")
root.mkdir(parents=True, exist_ok=True)
chrome = os.getenv("CHROMIUM_PATH") or shutil.which("chromium") or shutil.which("google-chrome")
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path=chrome,
                                args=["--no-sandbox", "--disable-dev-shm-usage"])
    page = browser.new_page()
    page.set_content("<h1>Tool System V2 Chrome CPU OK</h1>")
    assert page.get_by_role("heading", name="Tool System V2 Chrome CPU OK").count() == 1
    dest = root / "chrome_cpu_smoke.png"
    page.screenshot(path=str(dest))
    print(f"CHROME_CPU_PASS={dest.resolve()}")
    browser.close()
