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

### Run the platform inside Docker

The repository now ships with a `Dockerfile` plus a `compose.yaml` so you can keep the monitor, orchestrator, and GitHub workflow inside an isolated container while still reading logs from any other Docker environment.

```bash
# Build the image (optional when using docker compose up --build)
docker build -t dengi:latest .

# Linux/macOS (socket under /var/run/docker.sock)
docker compose up --build dengi
```

Important mounts/environment variables:

- `DOCKER_SOCKET_PATH` (defaults to `/var/run/docker.sock`) lets you point the container at any local socket path. Windows users outside WSL can instead run `docker run` with `--mount type=npipe,source=//./pipe/docker_engine,target=//./pipe/docker_engine`.
- `DOCKER_HOST`, `DOCKER_TLS_VERIFY`, and `DOCKER_CERT_PATH` are forwarded so you can target remote Docker daemons exactly the same way you would on the host. Mount your TLS directory (for example `-v $HOME/.docker:/certs/docker:ro`) and set `DOCKER_CERT_PATH=/certs/docker` if you rely on client certificates.
- `.:/app` keeps your working tree and `.git` metadata available inside the container so the hotfix workflow can continue creating and pushing branches.
- `.env` is loaded by Compose so Google/GitHub credentials are still available without baking them into the image.

`docker run` usage mirrors the compose file:

```bash
docker run --rm \
  --env-file .env \
  -e DOCKER_HOST \
  -e DOCKER_TLS_VERIFY \
  -e DOCKER_CERT_PATH=/certs/docker \
  -v "$(pwd)":/app \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$HOME/.docker":/certs/docker:ro \
  dengi:latest
```

To read logs from multiple environments at once, start additional containers with different names and `DOCKER_HOST` values (or sockets). Each instance of `DockerLogMonitor` will stream from the daemon it is pointed at, so you can run parallel monitors for staging, production, and local workloads.

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
- optional: `GITHUB_REPO_CLONE_URL` if you need to override the default `https://github.com/<owner>/<repo>.git` (useful for private repos or SSH URLs)
- optional: `HOTFIX_LOCAL_REPO` if you want to reuse an existing working tree instead of the default throwaway clone

By default, each hotfix run clones the target GitHub repo into a temporary directory, applies the AI patch there, pushes the branch/PR via the GitHub App API, and then deletes the workspace. Your checked-out repository is never mutated. Supplying `HOTFIX_LOCAL_REPO` (or the `repo_path` argument when calling `run_hotfix_workflow`) opts back into a persistent workspace if you need one.

The LLM agents now receive an abridged repository tree (up to two directory levels) pulled via the GitHub App credentials before proposing a fix, which helps them pick the right paths even when they have not seen the code before.

When an error does not include explicit file paths, the workflow automatically pulls both backend *and* frontend “hot spot” files (even if the log came from just one container) and places their contents under `[RELEVANT FILE CONTENTS]` in the LLM prompt. Specialists use those snippets to emit the full updated file contents for each edit, which avoids bad diffs and ensures `git apply` never needs to guess about context.

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

`main.py` now runs in fail-fast mode by default: if the agent orchestrator or hotfix workflow hits a fatal error, the Docker log monitor shuts down and the process exits with a non-zero status. Set `DENGI_FAIL_FAST=false` if you prefer the legacy behavior where the service keeps running despite failures.

To avoid spamming duplicate PRs for a noisy log, the `ContinuousHotfixPipeline` keeps a signature cache for each error (default 10-minute TTL). Adjust `DENGI_EVENT_TTL_SECONDS` if you want a longer or shorter cooldown before the same error can trigger another patch attempt.
