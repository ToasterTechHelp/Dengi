# backend_agent.py
from google.adk.agents import Agent

BackendAgent = Agent(
    model='gemini-2.5-flash',
    name='BackendAgent',
    description=(
        "Analyzes backend or server-side issues. "
        "Capable of reading error logs and suggesting code-level fixes "
        "for Python, Node.js, and general backend architectures."
    ),
    instruction=(
        "When assigned an error, inspect the stack trace or message. "
        "Determine root cause and generate a safe patch or improvement suggestion."
    )
)
