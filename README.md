# Dengi

## Monitor Service

The monitor service tails Docker container logs (stdout, stderr, historical logs) and flags suspected errors with a lightweight rule-based classifier. It also attempts to pull any stack trace that appears near the flagged log entry so downstream systems (e.g., an LLM) receive the full context.

### Run the monitor locally

```bash
# Linux/macOS
source .venv/bin/activate
pip install -r requirements.txt
python -m src.monitor.service

# Windows PowerShell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m src.monitor.service
```

By default the monitor:

- Watches every running container except itself.
- Waits 10 seconds on startup before tailing logs so containers can finish booting.
- Streams live stdout and stderr for each container (skips historical backlog).
- Emits structured events via a configurable handler (defaults to printing error logs to stdout).
- Forwards flagged errors to `agents.runAgents.run_agents` so the agent swarm can triage issues automatically.
- Uses `RuleBasedClassifier` and `StackTraceExtractor`, which can be swapped or extended for more advanced logic.

### Google ADK / Gemini credentials

The agent runners rely on Google ADK’s latest (Oct 2025) guidance for the `google.genai.Client`. Before starting `python -m src.agents.runAgents` or the monitor, define either:

- `GOOGLE_API_KEY` (or `GENAI_API_KEY`) for Gemini Developer API access, **or**
- `GOOGLE_GENAI_USE_VERTEXAI=true` plus `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` (optionally `GOOGLE_VERTEX_PROJECT/LOCATION`, which will be promoted automatically).

These variables can live in `.env`; `runAgents` loads them and raises a clear error if credentials are missing.

## GitHub hotfix workflow

`src/github/hotfix_workflow.py` keeps a shallow mirror of `main`, lets an AI callback edit files locally, and then uses the GitHub App credentials to push a feature branch plus PR. Configure these additional variables in `.env`:

- `GITHUB_REPO_OWNER` (or `GITHUB_ORG`)
- `GITHUB_REPO_NAME` (or `GITHUB_REPO`)
- optional: `GITHUB_BASE_BRANCH` (default `main`) and `GITHUB_REMOTE_NAME` (default `origin`)

Example usage:

```python
from pathlib import Path
from src.github.hotfix_workflow import run_hotfix_workflow

def ai_callback(repo_path: Path) -> str:
    # Inspect/modify files under repo_path ...
    return "Patched foo.py to guard against None inputs."

result = run_hotfix_workflow(
    reason_for_fix="null guard for foo service",
    error_details="Monitor detected AttributeError when foo() received None.",
    fix_details="Add None check before dereferencing foo.bar.",
    reasoning="Prevents crashes and matches API contract.",
    apply_changes=ai_callback,
)
print(f"Opened PR #{result['pr_number']} on branch {result['branch']}")
```

After each run the local checkout is reset back to the remote `main`, so the workspace is ready for the next incident. The resulting PR description contains the supplied error/fix/reasoning details plus the AI change summary.

## Continuous workflow (`main.py`)

`main.py` ties everything together:

1. Starts the Docker log monitor.
2. For each new error log, calls the LLM orchestrator (`run_agents`) to get a JSON patch plan.
3. Applies that plan to the shallow `main` mirror via `run_hotfix_workflow`, creating a `dengji/<slug>` branch and PR automatically.

Run it with:

```bash
python -m venv .venv
. .venv/bin/activate  # or .venv\Scripts\Activate.ps1 on Windows
pip install -r requirements.txt
python main.py
```

Ensure your `.env` includes both the Google GenAI credentials (for the agents) *and* the GitHub App variables listed above. The script will continue running until interrupted (Ctrl+C), and every successful AI fix results in a new branch/PR for humans to review.
