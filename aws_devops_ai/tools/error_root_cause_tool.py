"""ErrorRootCauseTool — high-resource agent for iterative root cause investigation."""

from __future__ import annotations

import logging
from pathlib import Path

from aws_devops_ai.models import (
    LogFinding,
    RootCauseReport,
    ToolResult,
    TopologyMap,
)
from aws_devops_ai.tool_registry import DevOpsTool, ModuleRegistry

logger = logging.getLogger(__name__)


class ErrorRootCauseTool(DevOpsTool):
    """Trace root cause of an error across connected resources."""

    name = "error_root_cause"
    description = "Trace root cause of an error across connected resources using logs and topology"
    parameters = {
        "target_arn": "str — ARN of the resource exhibiting the error (optional)",
        "error_pattern": "str — error pattern or message to investigate (optional)",
        "event_context": "dict — event metadata that triggered the investigation",
        "initial_findings": "list[LogFinding] — pre-existing findings to seed investigation (optional)",
    }

    _MAX_ITERATIONS = 6

    def execute(self, params: dict, modules: ModuleRegistry) -> ToolResult:
        from aws_devops_ai.infra.ask_kiro import KiroSession
        from aws_devops_ai.infra.file_readers import is_supported_file
        import json as _json

        # Load topology
        topo_path = Path(modules.config.topology_output_dir) / "topology.json"
        if topo_path.exists():
            from aws_devops_ai.models import TopologyMap
            topology = TopologyMap.from_dict(_json.loads(topo_path.read_text()))
        else:
            topology = modules.topology_manager.load()

        error_pattern = params.get("error_pattern", "")
        log_dir = params.get("log_dir", modules.config.log_dir)

        # Build index of available log files (all supported formats, recursive)
        log_dir_path = Path(log_dir)
        log_paths = sorted(
            f for f in log_dir_path.rglob("*")
            if f.is_file() and is_supported_file(f) and not f.name.startswith(".")
        ) if log_dir_path.is_dir() else []
        available_logs = [str(p.relative_to(log_dir_path)) for p in log_paths]
        log_path_map = {str(p.relative_to(log_dir_path)): p for p in log_paths}

        topo_summary = self._format_topology(topology)
        all_findings: list[LogFinding] = []

        # Start master agent session
        master = KiroSession(model=modules.config.high_resource_model, timeout=180, credit_tracker=modules.credit_tracker, label="rca-master")
        try:
            # Initial prompt — master decides which logs to pull first
            init_prompt = f"""You are a senior DevOps engineer investigating a production incident.
You have a topology map and a list of available log files. You do NOT have the log contents yet.
You will request logs, I will summarize them for you, and you will iterate until you can determine the root cause.

TRIGGER: "{error_pattern}"

TOPOLOGY:
{topo_summary}

AVAILABLE LOG FILES:
{chr(10).join(f"  - {name}" for name in available_logs)}

Which log files do you want to examine first? Pick the most relevant ones based on the trigger and topology.
Return a JSON object: {{"request_logs": ["filename1", "filename2"], "reasoning": "why these files"}}
Return ONLY the JSON object."""

            response = master.start(init_prompt)
            iteration = 0

            while iteration < self._MAX_ITERATIONS:
                iteration += 1

                # Parse master's request
                requested = self._parse_log_request(response)
                if not requested:
                    # Master didn't request logs — check if it returned a final report
                    report = self._parse_agent_response(response, all_findings, topology)
                    if report.root_cause_chain:
                        break
                    # No report either — ask master to conclude
                    response = master.send(
                        "You have all the data. Return your final analysis as a JSON object with: "
                        "root_cause_chain, confidence, hypothesis, narrative, affected_resources, suggested_remediation, "
                        "and source_evidence — each entry must have source_file, line_numbers, excerpt, relevance, and links_to. "
                        "Every claim must trace to specific lines in specific files. "
                        "Return ONLY the JSON object."
                    )
                    report = self._parse_agent_response(response, all_findings, topology)
                    break

                # Sub agent: analyze requested log files
                paths_to_analyze = []
                for name in requested:
                    if name in log_path_map:
                        paths_to_analyze.append(log_path_map[name])
                    else:
                        logger.warning("Master requested unknown log file: %s", name)

                if paths_to_analyze:
                    new_findings = modules.log_analyzer_agent.analyze(paths_to_analyze)
                    all_findings.extend(new_findings)

                    # Format new findings for master
                    findings_summary = self._format_findings(new_findings)
                    analyzed_names = [str(p.relative_to(log_dir_path)) for p in paths_to_analyze]
                else:
                    findings_summary = "(no matching log files found)"
                    analyzed_names = requested

                # Send findings back to master and ask for next action
                remaining = [n for n in available_logs if n not in [str(p.relative_to(log_dir_path)) for p in paths_to_analyze]]
                followup = f"""Here are the findings from {', '.join(analyzed_names)}:

{findings_summary}

REMAINING UNREAD LOG FILES:
{chr(10).join(f"  - {name}" for name in remaining) if remaining else "  (none)"}

Based on what you've learned, either:
1. Request more logs: {{"request_logs": ["filename"], "reasoning": "why"}}
2. Or return your final analysis as a JSON object with:
   - root_cause_chain: list of resource/component identifiers from root to surface
   - confidence: 0.0-1.0
   - hypothesis: one-sentence root cause summary
   - narrative: list of timeline entries explaining what happened step by step
   - affected_resources: list of resource identifiers
   - suggested_remediation: actionable fix
   - source_evidence: list of objects, each with:
     - "source_file": exact filename
     - "line_numbers": list of specific line numbers referenced
     - "excerpt": verbatim text from those lines
     - "relevance": how this evidence supports the conclusion
     - "links_to": which other source_evidence entries this connects to (by index) and why

CRITICAL: Your analysis is only trustworthy if EVERY claim traces back to specific lines in specific files.
Include the reasoning chain showing how evidence from different files connects to form the conclusion.

Return ONLY the JSON object."""

                response = master.send(followup)

            else:
                # Hit max iterations — force conclusion
                response = master.send(
                    "Max iterations reached. Return your final analysis now as a JSON object with: "
                    "root_cause_chain, confidence, hypothesis, narrative, affected_resources, suggested_remediation, "
                    "and source_evidence — each entry must have source_file, line_numbers, excerpt, relevance, and links_to. "
                    "Every claim must trace to specific lines in specific files. "
                    "Return ONLY the JSON object."
                )
                report = self._parse_agent_response(response, all_findings, topology)

        except Exception as e:
            logger.error("AI investigation failed: %s", e)
            report = RootCauseReport(
                hypothesis=f"Investigation failed: {e}",
                supporting_findings=all_findings,
                affected_resources=[arn for f in all_findings for arn in f.resource_arns],
            )
        finally:
            master.end()

        report.supporting_findings = all_findings
        report.iterations_used = iteration

        # Save report artifacts to rca_output
        output_dir = getattr(modules.config, 'rca_output_dir', None) or './rca_output'
        credit_summary = modules.credit_tracker.summary()
        saved = self.save_results(report, topology, output_dir, credit_summary)
        logger.info("RCA report saved: %s", saved)

        return ToolResult(
            tool_name=self.name,
            status="success",
            data=report,
            metadata={"credit_usage": credit_summary, "output_files": saved},
        )

    @staticmethod
    def _parse_log_request(response: str) -> list[str]:
        """Parse master agent's log request. Returns list of filenames, or empty if it's a final report."""
        import json as _json
        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                data = _json.loads(response[start:end])
                if "request_logs" in data:
                    return data["request_logs"]
        except (ValueError, _json.JSONDecodeError):
            pass
        return []

    @staticmethod
    def _format_topology(topology: TopologyMap) -> str:
        """Format topology as a concise text summary for the AI agent."""
        lines = []
        lines.append("Nodes:")
        for arn, node in topology.nodes.items():
            lines.append(f"  - {node.name} ({node.resource_type}) [{arn}]")
        lines.append("Edges:")
        for edge in topology.edges:
            src = topology.nodes.get(edge.source_arn)
            tgt = topology.nodes.get(edge.target_arn)
            src_name = src.name if src else edge.source_arn
            tgt_name = tgt.name if tgt else edge.target_arn
            lines.append(f"  - {src_name} --[{edge.relationship}]--> {tgt_name}")
        orphans = [
            arn for arn in topology.nodes
            if not any(e.source_arn == arn or e.target_arn == arn for e in topology.edges)
        ]
        if orphans:
            lines.append("Orphan nodes (no connections):")
            for arn in orphans:
                node = topology.nodes[arn]
                lines.append(f"  - {node.name} ({node.resource_type}) [{arn}]")
        return "\n".join(lines)

    @staticmethod
    def _format_findings(findings: list[LogFinding]) -> str:
        """Format findings as a concise text summary for the AI agent (capped at ~100k chars)."""
        lines = []
        seen = set()
        for f in findings:
            key = (f.message[:80], tuple(f.resource_arns))
            if key in seen:
                continue
            seen.add(key)
            ts = f.timestamp.strftime("%H:%M:%S") if f.timestamp else "??:??:??"
            arns = ", ".join(f.resource_arns) if f.resource_arns else "(no ARN)"
            line_ref = f"lines {f.line_numbers}" if f.line_numbers else "lines unknown"
            lines.append(f"  [{ts}] [{f.severity.value.upper():8s}] {f.message}")
            lines.append(f"           Source: {f.source_file} ({line_ref})")
            lines.append(f"           ARNs: {arns}")
            if f.raw_lines:
                for rl in f.raw_lines[:5]:
                    lines.append(f"           > {rl}")
        result = "\n".join(lines)
        if len(result) > 100_000:
            result = result[:100_000] + "\n... (truncated)"
        return result

    def _parse_agent_response(self, response: str, all_findings: list[LogFinding], topology: TopologyMap) -> RootCauseReport:
        """Parse the AI agent's JSON response into a RootCauseReport."""
        import json as _json

        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                data = _json.loads(response[start:end])
            else:
                raise ValueError("No JSON object found")
        except (ValueError, _json.JSONDecodeError) as e:
            logger.warning("Failed to parse agent response: %s", e)
            return RootCauseReport(
                hypothesis=response[:500],
                confidence=0.3,
                supporting_findings=all_findings,
            )

        # Normalize types — agent may return lists or strings
        remediation = data.get("suggested_remediation")
        if isinstance(remediation, list):
            remediation = "\n".join(str(r) for r in remediation)

        narrative = data.get("narrative", [])
        if isinstance(narrative, str):
            narrative = [narrative]
        narrative = [str(n) for n in narrative]

        return RootCauseReport(
            root_cause_chain=data.get("root_cause_chain", []),
            confidence=float(data.get("confidence", 0.5)),
            hypothesis=str(data.get("hypothesis", "")),
            narrative=narrative,
            supporting_findings=all_findings,
            affected_resources=data.get("affected_resources", []),
            iterations_used=1,
            suggested_remediation=remediation,
            source_evidence=data.get("source_evidence", []),
        )









    @staticmethod
    def save_report(report: RootCauseReport, output_path: str) -> str:
        """Persist a RootCauseReport to a JSON file and return the path."""
        import json
        from pathlib import Path

        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report.to_dict(), indent=2, default=str))
        return str(path)

    @staticmethod
    def print_summary(report: RootCauseReport, topology: TopologyMap) -> None:
        """Print RCA summary to stdout."""
        def _label(arn: str) -> str:
            node = topology.nodes.get(arn)
            return node.name if node else arn.split(":")[-1]

        print(f"\nConfidence: {report.confidence}")
        print(f"Iterations: {report.iterations_used}")

        if report.root_cause_chain:
            print("\n--- Root Cause Chain ---")
            for i, arn in enumerate(report.root_cause_chain):
                label = _label(arn)
                prefix = "ROOT ->" if i == 0 else "    ->"
                print(f"  {prefix} {label} ({arn})")

        if report.narrative:
            print("\n--- Incident Narrative ---")
            for line in report.narrative:
                print(f"  {line}")

        if report.suggested_remediation:
            print(f"\nRemediation: {report.suggested_remediation}")

    @staticmethod
    def save_results(report: RootCauseReport, topology: TopologyMap, output_dir: str, credit_summary: dict = None) -> dict[str, str]:
        """Save all RCA artifacts (JSON + markdown) to a directory."""
        from pathlib import Path

        Path(output_dir).mkdir(parents=True, exist_ok=True)
        paths: dict[str, str] = {}

        paths["json"] = ErrorRootCauseTool.save_report(
            report, str(Path(output_dir) / "rca_report.json")
        )
        paths["md"] = ErrorRootCauseTool.save_report_md(
            report, topology, str(Path(output_dir) / "incident_report.md"), credit_summary
        )
        return paths

    @staticmethod
    def save_report_md(report: RootCauseReport, topology: TopologyMap, output_path: str, credit_summary: dict = None) -> str:
        """Generate a human-readable markdown incident report and save it."""
        from datetime import datetime
        from pathlib import Path

        def _label(arn: str) -> str:
            node = topology.nodes.get(arn)
            return node.name if node else arn.split(":")[-1]

        def _svc(arn: str) -> str:
            node = topology.nodes.get(arn)
            return node.resource_type if node else "unknown"

        lines: list[str] = []

        # Header
        lines.append("# Incident Report")
        lines.append("")
        lines.append(f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
        lines.append(f"Confidence: {report.confidence}")
        lines.append(f"Investigation depth: {report.iterations_used} iterations")
        lines.append("")

        # Incident narrative
        lines.append("## Incident Timeline")
        lines.append("")
        if report.narrative:
            for entry in report.narrative:
                lines.append(entry)
            lines.append("")
        else:
            lines.append("No narrative could be constructed.")
            lines.append("")

        # Root cause chain
        lines.append("## Root Cause Chain")
        lines.append("")
        if report.root_cause_chain:
            for i, arn in enumerate(report.root_cause_chain):
                name = _label(arn)
                svc = _svc(arn)
                node = topology.nodes.get(arn)
                health = node.health if node else None
                status = health.status if health else "unknown"
                err_count = health.error_count if health else 0
                last_err = health.last_error if health else None

                if i == 0:
                    role = "ROOT CAUSE"
                elif i == len(report.root_cause_chain) - 1:
                    role = "SURFACE"
                else:
                    role = "PROPAGATION"

                lines.append(f"### {i + 1}. {name} ({svc}) — {role}")
                lines.append("")
                lines.append(f"- ARN: `{arn}`")
                lines.append(f"- Health: {status} ({err_count} errors)")
                if last_err:
                    lines.append(f"- Last error: {last_err}")
                lines.append("")
        else:
            lines.append("No root cause chain identified.")
            lines.append("")

        # Topology overview
        lines.append("## Topology Overview")
        lines.append("")

        # Nodes table
        lines.append("### Resources")
        lines.append("")
        lines.append("| Resource | Type | Health | Errors | Last Error |")
        lines.append("|----------|------|--------|--------|------------|")
        for arn, node in topology.nodes.items():
            h = node.health
            last = (h.last_error or "—")[:60]
            lines.append(f"| {node.name} | {node.resource_type} | {h.status} | {h.error_count} | {last} |")
        lines.append("")

        # Edges table
        lines.append("### Connections")
        lines.append("")
        lines.append("| Source | Relationship | Target |")
        lines.append("|--------|-------------|--------|")
        for edge in topology.edges:
            src = _label(edge.source_arn)
            tgt = _label(edge.target_arn)
            lines.append(f"| {src} | {edge.relationship} | {tgt} |")
        lines.append("")

        # Affected resources
        lines.append("## Affected Resources")
        lines.append("")
        for arn in report.affected_resources:
            name = _label(arn)
            svc = _svc(arn)
            lines.append(f"- {name} ({svc}) — `{arn}`")
        lines.append("")

        # Remediation
        lines.append("## Recommended Action")
        lines.append("")
        if report.suggested_remediation:
            lines.append(report.suggested_remediation)
        else:
            lines.append("No specific remediation suggested.")
        lines.append("")

        # Source Evidence
        lines.append("## Source Evidence")
        lines.append("")
        if report.source_evidence:
            for i, ev in enumerate(report.source_evidence):
                src = ev.get("source_file", "unknown")
                line_nums = ev.get("line_numbers", [])
                excerpt = ev.get("excerpt", "")
                relevance = ev.get("relevance", "")
                links = ev.get("links_to", "")
                line_ref = f" (lines {', '.join(str(n) for n in line_nums)})" if line_nums else ""
                lines.append(f"### Evidence {i}: `{src}`{line_ref}")
                lines.append("")
                if relevance:
                    lines.append(f"**Relevance:** {relevance}")
                    lines.append("")
                if excerpt:
                    lines.append("```")
                    lines.append(excerpt)
                    lines.append("```")
                    lines.append("")
                if links:
                    lines.append(f"**Links to:** {links}")
                    lines.append("")
        else:
            lines.append("No source evidence recorded.")
            lines.append("")

        # Credit Usage
        if credit_summary:
            lines.append("## Credit Usage")
            lines.append("")
            lines.append(f"| Metric | Value |")
            lines.append("|--------|-------|")
            lines.append(f"| Total sessions | {credit_summary.get('total_sessions', 0)} |")
            lines.append(f"| Total credits | {credit_summary.get('total_credits', 0)} |")
            lines.append(f"| Total time | {credit_summary.get('total_time_secs', 0)}s |")
            lines.append(f"| Total turns | {credit_summary.get('total_turns', 0)} |")
            lines.append("")
            sessions = credit_summary.get("sessions", [])
            if sessions:
                lines.append("### Session Breakdown")
                lines.append("")
                lines.append("| # | Agent | Model | Credits | Turns | Time |")
                lines.append("|---|-------|-------|---------|-------|------|")
                for i, s in enumerate(sessions):
                    label = s.get("label", f"session-{i}")
                    model = s.get("model", "unknown")
                    credits = s.get("total_credits", 0)
                    turns = s.get("turns", 0)
                    time_s = s.get("total_time_secs", 0)
                    lines.append(f"| {i+1} | {label} | {model} | {credits} | {turns} | {time_s}s |")
                lines.append("")

        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines))
        return str(path)
