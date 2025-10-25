# frontend_agent.py
from google.adk.agents import Agent

FrontendAgent = Agent(
    model='gemini-2.5-flash',
    name='FrontendAgent',
    description=(
        "Handles client-side and UI issues. "
        "Capable of diagnosing rendering problems, JavaScript errors, "
        "and UI behavior inconsistencies."
    ),
    instruction=(
        "When given a frontend error, analyze it and suggest a fix "
        "in the relevant component or HTML/JS file. "
        "Output a patch proposal or debug reasoning."
    )
)
