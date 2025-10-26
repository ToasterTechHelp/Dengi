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
