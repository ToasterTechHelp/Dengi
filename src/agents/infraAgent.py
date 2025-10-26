# infra_agent.py
from google.adk.agents import Agent

InfraAgent = Agent(
    model='gemini-2.5-flash',
    name='InfraAgent',
    description=(
        "Manages infrastructure, networking, or deployment errors. "
        "Can recommend docker fixes, service restarts, or configuration adjustments."
    ),
    instruction=(
        "When presented with an infrastructure error (timeouts, container crashes, "
        "or network failures), analyze logs and propose stability improvements."
    )
)
