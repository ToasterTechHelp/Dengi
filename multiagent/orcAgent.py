# orcAgent.py
import json
import re
from typing import Any, Dict, Optional
from google.genai import types

JSON_DECISION_SCHEMA = """\
Return ONLY a minified JSON object with this exact schema:
{"decision":"backend|frontend|infra"}"""

JSON_PATCH_SCHEMA = """\
Return ONLY a minified JSON object with this schema:
{
  "summary": "one-sentence description of the fix",
  "files": [
    {
      "path": "relative/path/to/file.ext",
      "patch": "unified diff or full file replacement instructions; MUST be directly applicable by a developer"
    }
  ],
  "patch": "optional unified diff spanning multiple files if you prefer",
  "notes": "optional extra context for the reviewer, keep short"
}
Do not include markdown fences or extra commentary. Output a single JSON object on one line.
"""

class OrchestratorAgent:
    def __init__(self, backend_runner, frontend_runner, infra_runner, dengi_runner, user_id: str, session_id: str):
        self.runners = {
            "backend": backend_runner,
            "frontend": frontend_runner,
            "infra": infra_runner,
        }
        self.dengi_runner = dengi_runner
        self.user_id = user_id
        self.session_id = session_id

    async def process(self, log: str) -> Dict[str, Any]:
        """
        1) Ask Dengi which specialist should handle this log (JSON decision).
        2) Ask the chosen specialist for a JSON patch plan.
        3) Return a single structured dict ready for GitHub PR creation.
        """
        # 1) Decide specialist
        decision_prompt = self._build_decision_prompt(log)
        decision_text = await self._call_runner(self.dengi_runner, decision_prompt)
        decision = self._parse_decision(decision_text) or "backend"  # safe fallback

        # 2) Get specialist patch JSON
        specialist_runner = self.runners.get(decision, self.runners["backend"])
        patch_prompt = self._build_patch_prompt(decision, log)
        patch_text = await self._call_runner(specialist_runner, patch_prompt)
        patch_json = self._safe_json(patch_text) or {
            "summary": "No structured patch returned.",
            "files": [],
            "notes": "Specialist returned empty/invalid JSON."
        }

        # 3) Combined result (what you’ll hand to your GitHub step)
        return {
            "chosen_agent": decision,
            "original_log": log,
            "patch_plan": patch_json  # strictly JSON per schema
        }

    # ---------------------------
    # Prompt builders
    # ---------------------------
    def _build_decision_prompt(self, log: str) -> str:
        return (
            "ROLE: You are Dengi Orchestrator. Decide which specialist should handle the log.\n\n"
            "[LOG EVENT]\n"
            f"{log}\n\n"
            "[INSTRUCTION]\n"
            "Select exactly ONE of: backend, frontend, infra.\n"
            f"{JSON_DECISION_SCHEMA}"
        )

    def _build_patch_prompt(self, decision: str, log: str) -> str:
        # Tailor a tiny bit by domain
        focus = {
            "backend": "server, database, API handlers, business logic",
            "frontend": "UI components, rendering, client-side JS/TS",
            "infra": "network, timeouts, container/deployment, service config"
        }.get(decision, "the relevant code")
        return (
            f"ROLE: You are the {decision} specialist. Focus on {focus}.\n\n"
            "[LOG EVENT]\n"
            f"{log}\n\n"
            "[INSTRUCTION]\n"
            "Produce a concrete patch plan the dev can apply. If you emit a unified diff, ensure paths are correct.\n"
            f"{JSON_PATCH_SCHEMA}"
        )

    # ---------------------------
    # Runner calling (ADK-version tolerant)
    # ---------------------------
    async def _call_runner(self, runner, message_text: str) -> str:
        """
        Calls a Runner with a proper ADA A2K message object and collects text output.
        Works across versions that return async generators, sync generators, or awaitables.
        """
        content = types.Content(role="user", parts=[types.Part(text=message_text)])
        stream = runner.run(
            user_id=self.user_id,
            session_id=self.session_id,
            new_message=content
        )

        # Case A: async generator
        if hasattr(stream, "__aiter__"):
            buf = []
            async for ev in stream:
                txt = self._event_text(ev)
                if txt:
                    buf.append(txt)
            return "".join(buf)

        # Case B: awaitable
        if hasattr(stream, "__await__"):
            result = await stream
            # Single event/object
            txt = self._event_text(result)
            if txt:
                return txt
            # Or iterable of events/objects
            try:
                return "".join(filter(None, (self._event_text(e) for e in result)))
            except TypeError:
                return str(result or "")

        # Case C: sync generator / iterable
        buf = []
        try:
            for ev in stream:
                txt = self._event_text(ev)
                if txt:
                    buf.append(txt)
            return "".join(buf)
        except TypeError:
            return str(stream or "")

    def _event_text(self, ev: Any) -> str:
        # Preferred: .output_text
        if hasattr(ev, "output_text") and ev.output_text:
            return ev.output_text

        # Fallback: content.parts[].text
        content = getattr(ev, "content", None)
        if content and hasattr(content, "parts"):
            texts = []
            for p in getattr(content, "parts", []) or []:
                t = getattr(p, "text", None)
                if t:
                    texts.append(t)
            if texts:
                return "".join(texts)

        # Last resort: stringified
        return ev if isinstance(ev, str) else ""

    # ---------------------------
    # JSON helpers
    # ---------------------------
    def _parse_decision(self, raw: str) -> Optional[str]:
        data = self._safe_json(raw)
        if not isinstance(data, dict):
            return None
        decision = str(data.get("decision", "")).strip().lower()
        if decision in ("backend", "frontend", "infra"):
            return decision
        # tiny normalization if model adds punctuation etc
        decision = re.split(r"[^a-z]+", decision)[0]
        return decision if decision in ("backend", "frontend", "infra") else None

    def _safe_json(self, raw: str) -> Optional[Dict[str, Any]]:
        """
        Lenient loader: handles models that wrap JSON in backticks or add text.
        """
        if not raw:
            return None
        raw = raw.strip()

        # Remove markdown fences if present
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.DOTALL).strip()

        # Extract first JSON object if extra chatter exists
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        snippet = raw[start : end + 1]

        try:
            return json.loads(snippet)
        except Exception:
            return None
