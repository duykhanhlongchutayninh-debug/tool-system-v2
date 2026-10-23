# Tool System V2

Tool System V2 ports the Kaggle Chrome/noVNC infrastructure and business workflow Steps 05–07 from Money AI into a separate repository.

## Kaggle dashboard

After the repository is present in `/kaggle/working/tool-system-v2` and runtime dependencies are installed, run one notebook cell:

```python
%cd /kaggle/working/tool-system-v2
!git pull --ff-only origin main
!python -m pip install -e .
!python -m pip install -r requirements-runtime.txt
from scripts.tool_system_dashboard import show_dashboard
show_dashboard()
```

The dashboard provides buttons for **Khởi động / kiểm tra Chrome**, **Mở màn hình Chrome**, and workflow Steps 05, 06, and 07. The Chrome launcher checks CDP 9222, reuses a recorded session if available, and refuses to start a second browser when an unknown Chrome process exists. The noVNC service is bound to loopback and the mobile link uses the authenticated Kaggle proxy.

## Business workflow copied from Money AI

- **Step 05:** verify/recover a Facebook source MP4, hash it, archive it to the configured private storage, and checkpoint `SOURCE_READY`.
- **Step 06:** use Gemini Web on the existing Chrome session to analyze the source and create a script, then verify the script SHA and state.
- **Step 07:** use Meta AI in the existing Chrome session, verify/download the MP4, archive it, and send the exact video for Telegram review. It does not publish to Facebook.

Every step retains the source workers' evidence and safety gates. The dashboard does not auto-run Steps 05–07.

## Separate-project setup

Tool System V2 intentionally does **not** use the old Money AI Supabase URL, service key, Telegram token, or Chrome backup data. Before running Step 05–07 or profile backup:

1. Create/use a separate Supabase project for Tool System V2.
2. Apply the four SQL files in `supabase/migrations/` to that new project.
3. In the dashboard, enter that project's URL and service key. Add a Telegram bot token and numeric owner chat ID for Step 07.
4. Use the Chrome screen link and sign in to the provider sites in this Tool System V2 Chrome profile.

Secrets entered in the dashboard are held in the current Kaggle kernel environment only; they are not written to repository files. Use Kaggle Secrets with the `TOOL_SYSTEM_...` names to re-load them after kernel reset.

## Important distinctions

- Successful code copy, unit tests, Chrome CDP, noVNC local HTTP, and Android screen access are separate checks.
- A link to noVNC is not proof that the phone opened the screen; open the generated link to verify it.
- A source file in the repo does not prove that Gemini, Meta AI, Supabase, or Telegram is authenticated or working at runtime.
- No real Facebook publish or scheduled post is included in Steps 05–07.
