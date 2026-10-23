# Tool System V2 — Kaggle runbook

## One-time notebook setup

In the Kaggle notebook named **Tool System V2**, run this one cell. It updates the checkout, installs runtime packages, syntax-checks the ported code, and displays the dashboard.

```python
%cd /kaggle/working/tool-system-v2
!git pull --ff-only origin main
!python -m pip install -e .
!python -m pip install -r requirements-runtime.txt
!python -m compileall -q src/money_ai scripts
from scripts.tool_system_dashboard import show_dashboard
show_dashboard()
```

If the repository is missing from the current Kaggle session, clone it first using the public URL in README.md.

## Dashboard controls

- **Khởi động / kiểm tra Chrome** calls `scripts/restore_one_chrome.py`, which uses `scripts/kaggle_cpu_desktop.py` to prepare display `:99`, a single headed Chromium process with CDP on loopback port 9222, and noVNC on loopback ports 5900/6080.
- **Mở màn hình Chrome** is generated using the active Kaggle Jupyter server when possible. If automatic discovery is unavailable, provide the current authenticated URL from **Run → Kaggle Jupyter Server → Copy VS Code Compatible URL**. Do not send that URL to anyone; it may contain a session token.
- **Sao lưu hồ sơ Chrome** is an explicit owner-triggered operation. It briefly stops and restarts only the validated Tool System V2 Chrome so the local profile can be archived; it requires the new project's schema and service key.
- **Bước 05** runs `scripts/run_business_step05.py`; **Bước 06** runs `scripts/run_business_step06.py`; **Bước 07** runs `scripts/run_business_step07.py`. Each button is separate and checks prerequisite state. Nothing is auto-published to Facebook.

## Supabase isolation

Do not set or reuse Money AI's old Supabase URL/key. Create a separate Supabase project, then apply these migrations in filename order using that new project's SQL editor or migration tool:

1. `supabase/migrations/20261023_tool_system_initial.sql`
2. `supabase/migrations/20261023_tool_system_storage.sql`
3. `supabase/migrations/20261023_tool_system_checkpoint_ledger.sql`
4. `supabase/migrations/20261023_tool_system_chrome_vault.sql`

Then enter the new project URL and service-role key in the dashboard. Telegram configuration is needed for Step 07 review delivery. The dashboard keeps entered values in the current kernel environment only. For persistence across kernel resets, create Kaggle Secrets named `TOOL_SYSTEM_SUPABASE_URL`, `TOOL_SYSTEM_SUPABASE_SERVICE_ROLE_KEY`, `TOOL_SYSTEM_TELEGRAM_BOT_TOKEN`, and `TOOL_SYSTEM_TELEGRAM_OWNER_CHAT_ID`.

Applying SQL migrations only changes the separate Tool System V2 database. The migration files do not transfer old database rows, old profile backups, cookies, API keys, or credentials.

## Evidence boundary

A code/test pass is not a runtime pass. Record these separately:
- Chrome CDP: `TOOL_SYSTEM_CHROME_CDP=PASS`
- Local noVNC HTTP: `TOOL_SYSTEM_SCREEN_LOCAL=PASS`
- Android link: only verified after the generated link opens and visibly shows the Chrome desktop on the phone
- Steps 05–07: require their own successful worker exit and source/script/video/checkpoint evidence
