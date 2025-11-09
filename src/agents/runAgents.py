import asyncio
import inspect
import os
from typing import Any, Callable

from dotenv import load_dotenv
from google.adk.sessions import InMemorySessionService
from google.adk.runners import Runner

from .agent import Dengi
from .backendAgent import BackendAgent
from .frontendAgent import FrontendAgent
from .infraAgent import InfraAgent
from .orcAgent import OrchestratorAgent

load_dotenv()

USER_ID = "hackathonDemo"
SESSION_ID = "hackathonDemoSession"
PRIMARY_APP_NAME = "DengiOrchestrator"
RUNNER_APP_NAME = "DengiOrchestratorApp"
BACKEND_APP = "BackendRunner"
FRONTEND_APP = "FrontendRunner"
INFRA_APP = "InfraRunner"
DENGI_APP = "DengiRunner"
SPECIALIST_APPS = [BACKEND_APP, FRONTEND_APP, INFRA_APP, DENGI_APP]


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _ensure_genai_credentials() -> None:
    """
    Ensures the Google GenAI client can authenticate using either the Gemini API key
    (`GOOGLE_API_KEY`) or Vertex AI env vars per Google ADK docs (2025-10).
    """
    # Accept alternate variable names for convenience.
    fallback_api_key = os.getenv("GENAI_API_KEY") or os.getenv("GOOGLE_GENAI_API_KEY")
    if fallback_api_key and not os.getenv("GOOGLE_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = fallback_api_key

    api_key = os.getenv("GOOGLE_API_KEY")
    if api_key:
        return

    project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GOOGLE_VERTEX_PROJECT")
    location = os.getenv("GOOGLE_CLOUD_LOCATION") or os.getenv("GOOGLE_VERTEX_LOCATION")
    use_vertex = _truthy(os.getenv("GOOGLE_GENAI_USE_VERTEXAI")) or (
        project is not None and location is not None
    )

    if use_vertex:
        if project and location:
            os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project)
            os.environ.setdefault("GOOGLE_CLOUD_LOCATION", location)
            os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")
            return
        missing = []
        if not project:
            missing.append("GOOGLE_CLOUD_PROJECT")
        if not location:
            missing.append("GOOGLE_CLOUD_LOCATION")
        raise RuntimeError(
            "Vertex AI configuration is incomplete. Set "
            + ", ".join(missing)
            + " or provide GOOGLE_API_KEY for the Gemini API."
        )

    raise RuntimeError(
        "Missing Google GenAI credentials. Define GOOGLE_API_KEY for the Gemini API "
        "or set GOOGLE_GENAI_USE_VERTEXAI=true with GOOGLE_CLOUD_PROJECT and "
        "GOOGLE_CLOUD_LOCATION for Vertex AI (per google.genai client docs 2025-10)."
    )


async def _maybe_await(call_result):
    """
    Await helper that supports sync or async ADK implementations.
    """
    if inspect.isawaitable(call_result):
        return await call_result
    return call_result


async def _call_session_method(
    session_service: InMemorySessionService,
    method_name: str,
    *,
    app_name: str | None,
    user_id: str,
    session_id: str,
) -> Any:
    method: Callable[..., Any] = getattr(session_service, method_name)
    kwargs = {"user_id": user_id, "session_id": session_id}
    if app_name is not None:
        kwargs["app_name"] = app_name
    try:
        result = method(**kwargs)
    except TypeError:
        kwargs.pop("app_name", None)
        result = method(**kwargs)
    return await _maybe_await(result)


async def _ensure_session_for_app(
    session_service: InMemorySessionService,
    *,
    app_name: str,
    user_id: str,
    session_id: str,
) -> None:
    existing = await _call_session_method(
        session_service,
        "get_session",
        app_name=app_name,
        user_id=user_id,
        session_id=session_id,
    )
    if existing:
        return
    try:
        await _call_session_method(
            session_service,
            "create_session",
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
        )
    except Exception:
        # If creation races or fails due to unsupported kwargs, retry a fetch.
        await _call_session_method(
            session_service,
            "get_session",
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
        )


async def _ensure_sessions(session_service: InMemorySessionService, user_id: str, session_id: str) -> None:
    """
    Ensures every runner has a backing session; creates one if missing.
    """
    for app_name in [PRIMARY_APP_NAME, RUNNER_APP_NAME, *SPECIALIST_APPS]:
        await _ensure_session_for_app(
            session_service,
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
        )


async def _build_orchestrator(user_id: str, session_id: str) -> OrchestratorAgent:
    _ensure_genai_credentials()
    session_service = InMemorySessionService()
    await _ensure_sessions(session_service, user_id, session_id)

    backend_runner = Runner(agent=BackendAgent, app_name=BACKEND_APP, session_service=session_service)
    frontend_runner = Runner(agent=FrontendAgent, app_name=FRONTEND_APP, session_service=session_service)
    infra_runner = Runner(agent=InfraAgent, app_name=INFRA_APP, session_service=session_service)
    dengi_runner = Runner(agent=Dengi, app_name=DENGI_APP, session_service=session_service)

    orchestrator = OrchestratorAgent(
        backend_runner=backend_runner,
        frontend_runner=frontend_runner,
        infra_runner=infra_runner,
        dengi_runner=dengi_runner,
        user_id=user_id,
        session_id=session_id,
    )
    return orchestrator


async def _run_agents_async(error_message: str) -> str:
    orchestrator = await _build_orchestrator(USER_ID, SESSION_ID)
    return await orchestrator.process(error_message)


def run_agents(error_message: str) -> str:
    """
    Entry point for the monitoring service. Accepts an error message and returns the agent analysis.
    """
    return asyncio.run(_run_agents_async(error_message))


async def main():
    print("Initializing Dengi multi-agent system...")
    orchestrator = await _build_orchestrator(USER_ID, SESSION_ID)
    print("Dengi and sub-agents are online.")

    test_logs = [
        "Database connection failed when fetching user records",
        "UI fails to render profile page correctly",
        "Server network timeout occurred during API request",
    ]

    for log in test_logs:
        analysis_result = await orchestrator.process(log)
        print("\n--- Log Analysis ---")
        print(analysis_result)


if __name__ == "__main__":
    asyncio.run(main())
