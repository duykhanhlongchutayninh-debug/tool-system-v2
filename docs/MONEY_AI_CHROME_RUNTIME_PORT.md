# Chrome CDP integration with Money AI's existing Kaggle session

Tool System V2's Chrome check is **attach-only**. Run it only inside the same
Kaggle notebook kernel/session that already owns the existing Chrome on CDP
port 9222. It does not start Chrome, Xvfb, VNC, or a new profile.

- Code is kept in the separate `tool-system-v2` repository.
- No Money AI business steps are imported or changed.
- No old Supabase, Chrome Vault, browser cookies, tokens, or credentials are copied.
- A successful CDP attach proves only that the browser is reachable; it does not
  prove Gemini/Meta login or permit publishing.

In the existing Money-AI-CPU Kaggle notebook, add one new Code cell and run only:

```python
import os, subprocess
repo = "/kaggle/working/tool-system-v2"
if not os.path.isdir(os.path.join(repo, ".git")):
    subprocess.run([
        "git", "clone",
        "https://github.com/duykhanhlongchutayninh-debug/tool-system-v2.git",
        repo
    ], check=True)
else:
    subprocess.run(["git", "-C", repo, "pull", "--ff-only", "origin", "main"], check=True)
subprocess.run(["python", "-m", "pip", "install", "-e", repo], check=True)
subprocess.run(["python", "-m", "pip", "install", "-r", repo + "/requirements-runtime.txt"], check=True)
subprocess.run(["python", repo + "/scripts/restore_one_chrome.py"], check=True)
```

This command only clones/updates Tool System V2, installs Playwright, and tries a
read-only attach to the existing `127.0.0.1:9222`. It never launches a browser.
If CDP is absent in this exact kernel, it fails closed; do not start a second
Chrome to make the check pass.
