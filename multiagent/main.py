import asyncio
from google.adk.sessions import InMemorySessionService
from google.adk.runners import Runner

from agent import Dengi
from backendAgent import BackendAgent
from frontendAgent import FrontendAgent
from infraAgent import InfraAgent
from orcAgent import OrchestratorAgent


async def main():
    print("🚀 Initializing Dengi multi-agent system...")

    # Shared session service for all runners
    session_service = InMemorySessionService()
    user_id = "hackathonDemo"
    session_id = "hackathonDemoSession"

    session_service.get_session(
        user_id=user_id,
        session_id=session_id,
        app_name="DengiOrchestrator"
    )
        # After session_service = InMemorySessionService()
    # and inside your async main():
    try:
        # if your build exposes an async get_session
        await session_service.get_session(user_id=user_id, session_id=session_id, app_name="DengiOrchestratorApp")
    except TypeError:
        # older builds: sync method or different signature
        session_service.get_session(user_id=user_id, session_id=session_id, app_name="DengiOrchestratorApp")
    except AttributeError:
        # very old builds: fall back to no app_name
        try:
            await session_service.get_session(user_id=user_id, session_id=session_id)
        except TypeError:
            session_service.get_session(user_id=user_id, session_id=session_id)



    # Wrap sub-agents in runners
    backend_runner = Runner(agent=BackendAgent, app_name="BackendRunner", session_service=session_service)
    frontend_runner = Runner(agent=FrontendAgent, app_name="FrontendRunner", session_service=session_service)
    infra_runner = Runner(agent=InfraAgent, app_name="InfraRunner", session_service=session_service)
    dengi_runner = Runner(agent=Dengi, app_name="DengiRunner", session_service=session_service)

    # Create orchestrator with reasoning
    orchestrator = OrchestratorAgent(
        backend_runner=backend_runner,
        frontend_runner=frontend_runner,
        infra_runner=infra_runner,
        dengi_runner=dengi_runner,
        user_id=user_id,
        session_id=session_id
    )

    print("✅ Dengi and sub-agents are online.")

    # Example logs to test dynamic routing
    test_logs = [
        "Database connection failed when fetching user records",
        "UI fails to render profile page correctly",
        "Server network timeout occurred during API request"
    ]

    # Process each log through orchestrator
    for log in test_logs:
        analysis_result = await orchestrator.process(log)
        print("\n--- Log Analysis ---")
        print(analysis_result)


if __name__ == "__main__":
    asyncio.run(main())
