# Chrome runtime adapted from Money AI

This port copies the Kaggle desktop/CDP launcher approach from
`maiduan2589-prog/money-ai` into Tool System V2, with strict project separation.

- Runs only on Kaggle CPU.
- Uses Tool System V2's own profile under `/kaggle/working/tool-system-v2-runtime`.
- Reuses its own recorded CDP Chrome if it is still present; refuses an unknown
  Chrome process already running without CDP metadata.
- Uses the Money AI noVNC proxy patch and bounded dependency checks.
- Does **not** import Money AI's business steps, old Supabase, Chrome Vault,
  browser cookies, tokens, or credentials.
- If the local profile is absent, it creates a new profile. Login status must be
  verified by visually inspecting the browser; code copying is not profile restore.
- Does not navigate to Gemini/Meta/Facebook, click provider actions, or publish.

Run `python -m pip install -e .`, then in a Kaggle code cell:
```python
%cd /kaggle/working/tool-system-v2
!python scripts/restore_one_chrome.py
```

Do not use `Run All` if other cells contain old browser-launch code. Run this
one cell only. A successful result proves CDP attach, not provider login.
