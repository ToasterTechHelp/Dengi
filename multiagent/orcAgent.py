# ============================
# Dengi Orchestrator Agent
# ============================
# This is the main coordinator for Dengi.
# It reads logs, detects errors, classifies them,
# and delegates them to specialized mock agents (backend, frontend, infra).

import os
import re
import time
import json
from typing import List, Dict, Any


class OrchestratorAgent:
    def __init__(self, codebase_path: str, log_path: str):
        self.codebase_path = codebase_path
        self.log_path = log_path

        # Mock sub-agents for demo purposes
        self.sub_agents = {
            'backend': self._mock_backend_agent,
            'frontend': self._mock_frontend_agent,
            'infra': self._mock_infra_agent
        }

    # ---------------------------
    # Core Functions
    # ---------------------------

    def scan_logs(self) -> str:
        """Reads the log file and returns its contents."""
        if not os.path.exists(self.log_path):
            return ""
        with open(self.log_path, 'r') as f:
            return f.read()

    def extract_errors(self, logs: str) -> List[str]:
        """Extracts lines that contain errors or exceptions."""
        pattern = re.compile(r"(ERROR: .*|Exception: .*|Traceback.*|TypeError: .*|ReferenceError: .*)")
        return pattern.findall(logs)

    def classify_error(self, error: str) -> str:
        """Classifies which sub-agent should handle the error."""
        lower = error.lower()
        if 'database' in lower or 'sql' in lower:
            return 'backend'
        elif 'ui' in lower or 'render' in lower or 'html' in lower:
            return 'frontend'
        elif 'timeout' in lower or 'docker' in lower or 'network' in lower:
            return 'infra'
        else:
            return 'backend'  # default for demo

    def handle_error(self, error: str) -> Dict[str, Any]:
        """Routes the error to the appropriate sub-agent."""
        agent_type = self.classify_error(error)
        handler = self.sub_agents.get(agent_type, self._mock_backend_agent)
        result = handler(error)
        return {
            'error': error,
            'agent': agent_type,
            'response': result
        }

    def run_cycle(self) -> Dict[str, Any]:
        """Main orchestrator function — one cycle of scanning and routing errors."""
        logs = self.scan_logs()
        if not logs:
            return {'status': 'no_logs_found', 'timestamp': time.time()}

        errors = self.extract_errors(logs)
        if not errors:
            return {'status': 'no_errors_found', 'timestamp': time.time()}

        actions = [self.handle_error(err) for err in errors]

        report = {
            'timestamp': time.time(),
            'error_count': len(errors),
            'actions': actions
        }

        # Save structured report for debugging/demo
        out_path = os.path.join(os.path.dirname(self.log_path), 'dengi_report.json')
        with open(out_path, 'w') as f:
            json.dump(report, f, indent=2)

        return report

    # ---------------------------
    # Mock Sub-Agents (Simplified)
    # ---------------------------

    def _mock_backend_agent(self, error: str) -> Dict[str, str]:
        return {
            'action': 'analyze_backend_error',
            'summary': f'Suggest patch for backend issue: {error[:60]}...'
        }

    def _mock_frontend_agent(self, error: str) -> Dict[str, str]:
        return {
            'action': 'fix_frontend_render',
            'summary': f'Suggest UI patch for: {error[:60]}...'
        }

    def _mock_infra_agent(self, error: str) -> Dict[str, str]:
        return {
            'action': 'restart_container_or_optimize_timeout',
            'summary': f'Handle infra issue: {error[:60]}...'
        }


if __name__ == "__main__":
    # Example usage
    orchestrator = OrchestratorAgent(
        codebase_path="../webapp",
        log_path="../webapp/logs/app.log"
    )
    result = orchestrator.run_cycle()
    print(json.dumps(result, indent=2))
