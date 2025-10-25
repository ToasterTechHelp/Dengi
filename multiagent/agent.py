# Root Dengi setup using Google ADA A2K

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.sessions import InMemorySessionService
from google.adk.runners import Runner
from google.genai import types
from orcAgent import OrchestratorAgent

import warnings
import logging
import asyncio
import os

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.ERROR)
print("ADA A2K libraries imported successfully.")

# Define your root agent
Dengi = Agent(
    model='gemini-2.5-flash',
    name='OrchestratorAgent',
    description=(
        "The central coordinator of Dengi's system. "
        "It reads logs, classifies errors, and assigns them "
        "to specialized agents such as backend, frontend, and infra."
    ),
    instruction=(
        "When an error occurs, analyze the log context, "
        "classify the error, and route it to the correct sub-agent."
    ),
)

orch = OrchestratorAgent(codebase_path="../webapp", log_path="../webapp/logs/app.log")
Dengi.add_tool(
    name="run_orchestration_cycle",
    description="Scans logs, classifies errors, and assigns them to the proper sub-agent.",
    func=orch.run_cycle
)