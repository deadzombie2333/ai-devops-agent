"""Investigation Context File — writes dynamic context to a temp file for AI sessions.

Instead of stuffing all context into the prompt string (which wastes tokens on
every turn), we write a context file to the session's cwd. The AI agent reads
it once, and subsequent prompts only carry new information.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from aws_devops_ai.models import InvestigationState, TopologyMap


class InvestigationContextWriter:
    """Writes and updates an investigation_context file in a working directory."""

    FILENAME = "investigation_context.txt"

    def __init__(self, work_dir: str) -> None:
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.work_dir / self.FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def write_initial(
        self,
        error_pattern: str,
        available_logs: list[str],
        topology: TopologyMap | None = None,
        state: InvestigationState | None = None,
    ) -> str:
        """Write the initial context file. Returns the file path."""
        lines = []
        lines.append(f"# Investigation Context")
        lines.append(f"# Generated: {datetime.utcnow().isoformat()}")
        lines.append("")

        # Trigger
        lines.append("## Trigger")
        lines.append(error_pattern or "(no specific error pattern)")
        lines.append("")

        # Topology
        lines.append("## Service Topology")
        if topology and topology.nodes:
            lines.append(f"{len(topology.nodes)} nodes, {len(topology.edges)} edges")
            lines.append("")
            lines.append("Nodes:")
            for arn, node in topology.nodes.items():
                lines.append(f"  - {node.name} ({node.resource_type}) [{arn}]")
            lines.append("")
            lines.append("Edges:")
            for edge in topology.edges:
                src = topology.nodes.get(edge.source_arn)
                tgt = topology.nodes.get(edge.target_arn)
                src_name = src.name if src else edge.source_arn
                tgt_name = tgt.name if tgt else edge.target_arn
                lines.append(f"  - {src_name} --[{edge.relationship}]--> {tgt_name}")
        else:
            lines.append("(no topology available)")
        lines.append("")

        # Available logs
        lines.append("## Available Log Files")
        for name in available_logs:
            lines.append(f"  - {name}")
        lines.append("")

        # Resumed state
        if state and state.iteration > 0:
            lines.append("## Resumed Investigation State")
            lines.append(f"Iteration: {state.iteration}/{state.max_iterations}")
            lines.append(f"Logs already analyzed: {', '.join(sorted(state.investigated_logs)) or '(none)'}")
            if state.hypothesis:
                lines.append(f"Current hypothesis: {state.hypothesis}")
            lines.append("")

        # Rules
        lines.append("## Investigation Rules")
        lines.append("- Request logs by returning: {\"request_logs\": [\"file1\", \"file2\"], \"reasoning\": \"why\"}")
        lines.append("- Every claim must trace to specific lines in specific files")
        lines.append("- When done, return a JSON report with: root_cause_chain, confidence, hypothesis,")
        lines.append("  narrative, affected_resources, suggested_remediation, source_evidence")
        lines.append("")

        content = "\n".join(lines)
        self._path.write_text(content)
        return str(self._path)

    def append_analysis(self, analysis_text: str, analyzed_files: list[str]) -> None:
        """Append analysis text to the context file."""
        if not analysis_text.strip():
            return

        lines = []
        lines.append("")
        lines.append(f"## Analysis Update ({datetime.utcnow().strftime('%H:%M:%S')})")
        lines.append(f"Analyzed: {', '.join(analyzed_files)}")
        lines.append("")
        lines.append(analysis_text)

        with open(self._path, "a") as f:
            f.write("\n".join(lines))
