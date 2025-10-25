# agent.py
from google.adk.agents import Agent



# Root orchestrator agent
Dengi = Agent(
    model="gemini-2.5-flash",
    name="DengiOrchestrator",
    description="Coordinates multiple specialized agents for debugging and analysis.",
    instruction=(
        "Receive logs, classify issues, "
        "and route them to the correct specialized agent: backend, frontend, or infra."
    )
)

print("✅ Dengi orchestrator initialized.")
