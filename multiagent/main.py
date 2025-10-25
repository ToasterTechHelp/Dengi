# main.py
from agent import Dengi
import asyncio

async def main():
    print("Running Dengi Orchestrator via ADA A2K...")
    result = await Dengi.run_tool("run_orchestration_cycle", {})
    print(result)

if __name__ == "__main__":
    asyncio.run(main())
