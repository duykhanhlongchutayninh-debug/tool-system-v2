# Tool System

Python tool scaffold for Kaggle CPU. It reuses an already-running Chrome through Chrome DevTools Protocol (CDP) and must never launch a second Chrome instance.

## Current scope
- Check whether an existing CDP endpoint is reachable.
- Represent job states with a small, explicit state set.
- Keep the initial test suite local and side-effect-free.

The video workflow, Gemini/Meta UI actions, persistence, scheduling, and Facebook publishing are **not implemented yet**. No real publishing or schedule activation is performed by this scaffold.

## Requirements
- Python 3.10+
- An existing Chrome instance with remote debugging enabled on port 9222, if testing CDP connectivity.

## Quick check
From the repository root:

```bash
python -m unittest discover -s tests -v
python -m tool_system cdp-check
```

The CDP check only queries the existing endpoint at `http://127.0.0.1:9222/json/version`; it does not start Chrome. A failed check is reported as failed, not retried indefinitely.
