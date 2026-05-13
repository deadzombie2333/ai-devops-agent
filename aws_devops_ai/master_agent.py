"""Master Agent — human-facing orchestrator that routes tasks to tools."""

from __future__ import annotations

import json
import logging

from aws_devops_ai.infra.ask_claude import ClaudeSession as KiroSession
from aws_devops_ai.models import SystemConfig
from aws_devops_ai.cli import build_registry

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a DevOps AI assistant. You help engineers investigate incidents, understand service topology, and answer operational questions.

You have access to these tools:
1. topology_update — Build or update a service topology map from log files. Use when the user asks about service connections, architecture, or wants to map their infrastructure.
2. error_root_cause — Investigate an incident by reading logs and tracing the root cause. Use when the user reports an error, timeout, outage, or asks "why is X failing?"

The tools support many file formats: .log, .csv, .rpt, .xel, .docx, .pdf, .txt, .json, .xml — not just .log files. They scan directories recursively.

IMPORTANT RULES:
- When the user asks to analyze logs or build topology, ALWAYS invoke the tool immediately. Do NOT ask for more information.
- Use the configured log_dir as the default — it is already set up.
- Return ONLY a JSON object for every response, no other text.

JSON formats:
- To invoke a tool: {"action": "tool", "tool": "tool_name", "params": {"log_dir": "path"}}
- To respond to user: {"action": "respond", "message": "your answer"}"""


class MasterAgent:
    """Human-facing agent that routes tasks to tools and manages conversation."""

    def __init__(self, config: SystemConfig) -> None:
        self.config = config
        self.registry, self.modules = build_registry(config)
        self._session: KiroSession | None = None
        self._history: list[dict] = []

    def start(self) -> str:
        """Start an interactive session. Returns a greeting."""
        # Load topology context if available
        topo_context = self._get_topology_context()

        self._session = KiroSession(model=self.config.high_resource_model, timeout=120, credit_tracker=self.modules.credit_tracker, label="master-agent")
        init_prompt = _SYSTEM_PROMPT

        # Tell the LLM where logs are
        init_prompt += f"\n\nCONFIGURATION:\n- Log directory: {self.config.log_dir}"
        if self.config.sources:
            source_list = ", ".join(s.identifier for s in self.config.sources)
            init_prompt += f"\n- Log sources: {source_list}"

        if topo_context:
            init_prompt += f"\n\nCURRENT TOPOLOGY:\n{topo_context}"
        else:
            init_prompt += "\n\nNo topology has been built yet."

        init_prompt += "\n\nGreet the user briefly. Keep it to one sentence."

        response = self._session.start(init_prompt)
        greeting = self._parse_response(response)
        return greeting

    def send(self, user_message: str) -> str:
        """Process a user message. Returns the agent's response."""
        if not self._session:
            self.start()

        self._history.append({"role": "user", "message": user_message})
        response = self._session.send(user_message)
        result = self._handle_response(response)
        self._history.append({"role": "assistant", "message": result})
        return result

    def end(self) -> None:
        """End the session."""
        if self._session:
            self._session.end()
            self._session = None

    def _handle_response(self, response: str) -> str:
        """Parse agent response and execute tools if needed."""
        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(response[start:end])
            else:
                return response.strip()
        except json.JSONDecodeError:
            return response.strip()

        action = data.get("action", "respond")

        if action == "respond":
            return data.get("message", response.strip())

        if action == "tool":
            tool_name = data.get("tool", "")
            params = data.get("params", {})

            # Set defaults
            if "log_dir" not in params:
                params["log_dir"] = self.config.log_dir

            # Wire up event stream for real-time progress
            from aws_devops_ai.models import AnalysisEvent
            params["on_event"] = lambda evt: print(f"  {evt.message}")

            print(f"\n[Running {tool_name}...]")
            result = self.registry.invoke(tool_name, params)

            if result.status == "success":
                # Feed result back to the agent for summarization
                summary = self._summarize_tool_result(tool_name, result)
                followup = (
                    f"Tool '{tool_name}' completed successfully. Here's the result:\n\n"
                    f"{summary}\n\n"
                    "Summarize this for the user in a clear, actionable way. "
                    'Return: {"action": "respond", "message": "your summary"}'
                )
                response2 = self._session.send(followup)
                return self._handle_response(response2)
            else:
                return f"Tool '{tool_name}' failed: {result.metadata}"

        return response.strip()

    def _parse_response(self, response: str) -> str:
        """Extract message from agent response."""
        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(response[start:end])
                if data.get("action") == "respond":
                    return data.get("message", response.strip())
        except json.JSONDecodeError:
            pass
        return response.strip()

    def _get_topology_context(self) -> str:
        """Load topology summary if available."""
        from pathlib import Path
        topo_path = Path(self.config.topology_output_dir) / "topology.json"
        if not topo_path.exists():
            return ""
        try:
            topo = self.modules.topology_manager.load()
            lines = []
            for arn, node in topo.nodes.items():
                lines.append(f"  {node.name} ({node.resource_type}) [{arn}]")
            for edge in topo.edges:
                src = topo.nodes.get(edge.source_arn)
                tgt = topo.nodes.get(edge.target_arn)
                src_name = src.name if src else edge.source_arn
                tgt_name = tgt.name if tgt else edge.target_arn
                lines.append(f"  {src_name} --[{edge.relationship}]--> {tgt_name}")
            return "\n".join(lines)
        except Exception:
            return ""

    def _summarize_tool_result(self, tool_name: str, result) -> str:
        """Create a concise summary of a tool result for the agent."""
        data = result.data
        if tool_name == "topology_update":
            return (
                f"Topology built: {len(data.topology.nodes)} nodes, "
                f"{len(data.topology.edges)} edges from {data.new_logs_downloaded} log files"
            )
        if tool_name == "error_root_cause":
            report = data
            lines = [f"Confidence: {report.confidence}"]
            if report.root_cause_chain:
                lines.append(f"Root cause chain: {' → '.join(report.root_cause_chain)}")
            if report.narrative:
                for n in report.narrative[:5]:
                    lines.append(str(n))
            if report.source_evidence:
                lines.append("\nSource Evidence:")
                for i, ev in enumerate(report.source_evidence):
                    src = ev.get("source_file", "unknown")
                    line_nums = ev.get("line_numbers", [])
                    excerpt = ev.get("excerpt", "")[:300]
                    relevance = ev.get("relevance", "")
                    links = ev.get("links_to", "")
                    line_ref = f"lines {line_nums}" if line_nums else ""
                    lines.append(f"  [{i}] {src} {line_ref}")
                    lines.append(f"      Relevance: {relevance}")
                    if excerpt:
                        lines.append(f"      Excerpt: {excerpt}")
                    if links:
                        lines.append(f"      Links to: {links}")
            if report.suggested_remediation:
                lines.append(f"\nRemediation: {report.suggested_remediation[:500]}")
            return "\n".join(lines)
        return str(data)[:1000]
