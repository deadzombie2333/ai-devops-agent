"""ErrorRootCauseTool — iterative root cause investigation.

Uses a master agent (high-resource model) that decides which logs to examine,
and a log analyzer agent (low-resource model) that reads and summarizes them.
All analysis is plain text — no intermediate JSON serialization between agents.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from aws_devops_ai.models import (
    AnalysisEvent,
    AnalysisEventType,
    InvestigationState,
    RootCauseReport,
    ToolResult,
    TopologyMap,
)
from aws_devops_ai.tool_registry import DevOpsTool, ModuleRegistry

logger = logging.getLogger(__name__)

EventCallback = Callable[[AnalysisEvent], None]


def _default_callback(event: AnalysisEvent) -> None:
    prefix = {
        AnalysisEventType.INVESTIGATION_STARTED: "🔍",
        AnalysisEventType.LOG_FILE_READING: "📖",
        AnalysisEventType.LOG_FILE_ANALYZED: "✅",
        AnalysisEventType.FINDINGS_DISCOVERED: "💡",
        AnalysisEventType.HYPOTHESIS_FORMED: "🧠",
        AnalysisEventType.REQUESTING_LOGS: "📋",
        AnalysisEventType.ITERATION_COMPLETE: "🔄",
        AnalysisEventType.CHECKPOINT_SAVED: "💾",
        AnalysisEventType.CONTEXT_FILE_WRITTEN: "📝",
        AnalysisEventType.INVESTIGATION_COMPLETE: "🏁",
        AnalysisEventType.ERROR: "❌",
    }.get(event.event_type, "  ")
    print(f"  {prefix} {event.message}")


class ErrorRootCauseTool(DevOpsTool):
    name = "error_root_cause"
    description = "Trace root cause of an error across connected resources using logs and topology"
    parameters = {
        "error_pattern": "str — error pattern or message to investigate (optional)",
        "on_event": "callable — callback for AnalysisEvent stream (optional)",
        "resume": "bool — attempt to resume from last checkpoint (default: False)",
    }

    _MAX_ITERATIONS = 6

    def execute(self, params: dict, modules: ModuleRegistry) -> ToolResult:
        from aws_devops_ai.infra.ask_claude import ClaudeSession
        from aws_devops_ai.infra.file_readers import is_supported_file
        import json as _json

        on_event: EventCallback = params.get("on_event", _default_callback)
        resume = params.get("resume", False)

        # Load topology
        topo_path = Path(modules.config.topology_output_dir) / "topology.json"
        if topo_path.exists():
            topology = TopologyMap.from_dict(_json.loads(topo_path.read_text()))
        else:
            topology = modules.topology_manager.load()

        error_pattern = params.get("error_pattern", "")
        log_dir = params.get("log_dir", modules.config.log_dir)
        output_dir = getattr(modules.config, 'rca_output_dir', None) or './rca_output'
        checkpoint_path = str(Path(output_dir) / "investigation_checkpoint.json")

        # Build index of available log files
        log_dir_path = Path(log_dir)
        log_paths = sorted(
            f for f in log_dir_path.rglob("*")
            if f.is_file() and is_supported_file(f) and not f.name.startswith(".")
        ) if log_dir_path.is_dir() else []
        available_logs = [str(p.relative_to(log_dir_path)) for p in log_paths]
        log_path_map = {str(p.relative_to(log_dir_path)): p for p in log_paths}

        # Checkpoint resume
        state: InvestigationState | None = None
        if resume:
            state = InvestigationState.load(checkpoint_path)
            if state and not state.is_resolved:
                on_event(AnalysisEvent(
                    AnalysisEventType.INVESTIGATION_STARTED,
                    f"Resuming from iteration {state.iteration} "
                    f"({len(state.investigated_logs)} logs analyzed)",
                    {"resumed": True, "iteration": state.iteration},
                ))
                remaining_logs = [n for n in available_logs if n not in state.investigated_logs]
            else:
                state = None

        if state is None:
            state = InvestigationState(
                max_iterations=self._MAX_ITERATIONS,
                error_pattern=error_pattern,
                log_dir=log_dir,
            )
            remaining_logs = available_logs
            on_event(AnalysisEvent(
                AnalysisEventType.INVESTIGATION_STARTED,
                f"Starting investigation: {len(available_logs)} log files available",
                {"log_count": len(available_logs), "error_pattern": error_pattern},
            ))

        # All analysis text accumulated across iterations
        all_analysis_text: list[str] = []

        # Topology summary for the master agent
        topo_summary = self._format_topology(topology)

        # Start master agent session
        master = ClaudeSession(
            model=modules.config.high_resource_model,
            credit_tracker=modules.credit_tracker,
            label="rca-master",
        )
        report = RootCauseReport()
        iteration = state.iteration

        try:
            init_prompt = (
                f"You are a senior DevOps engineer investigating a production incident.\n\n"
                f"TRIGGER: \"{error_pattern}\"\n\n"
                f"TOPOLOGY:\n{topo_summary}\n\n"
                f"AVAILABLE LOG FILES:\n"
                + "\n".join(f"  - {name}" for name in remaining_logs)
                + "\n\nWhich log files do you want to examine first? Pick the most relevant ones.\n"
                "Return a JSON object: {\"request_logs\": [\"filename1\", \"filename2\"], \"reasoning\": \"why\"}\n"
                "Return ONLY the JSON object."
            )

            response = master.start(init_prompt)

            while iteration < self._MAX_ITERATIONS:
                iteration += 1
                state.iteration = iteration

                requested = self._parse_log_request(response)
                if not requested:
                    report = self._parse_agent_response(response, topology)
                    if report.root_cause_chain:
                        break
                    response = master.send(
                        "You have all the data. Return your final analysis as a JSON object with: "
                        "root_cause_chain, confidence, hypothesis, narrative, affected_resources, "
                        "suggested_remediation, and source_evidence. "
                        "Return ONLY the JSON object."
                    )
                    report = self._parse_agent_response(response, topology)
                    break

                on_event(AnalysisEvent(
                    AnalysisEventType.REQUESTING_LOGS,
                    f"Iteration {iteration}: requesting {len(requested)} log files",
                    {"iteration": iteration, "files": requested},
                ))

                # Let the analyzer agent read and summarize the requested files
                paths_to_analyze = []
                for name in requested:
                    if name in log_path_map:
                        paths_to_analyze.append(log_path_map[name])
                    else:
                        logger.warning("Master requested unknown log file: %s", name)

                if paths_to_analyze:
                    on_event(AnalysisEvent(
                        AnalysisEventType.LOG_FILE_READING,
                        f"Analyzing {len(paths_to_analyze)} files...",
                        {"files": [str(p) for p in paths_to_analyze]},
                    ))

                    # analyzer.analyze() returns plain text now
                    analysis_text = modules.log_analyzer_agent.analyze(paths_to_analyze)
                    all_analysis_text.append(analysis_text)
                    analyzed_names = [str(p.relative_to(log_dir_path)) for p in paths_to_analyze]

                    state.investigated_logs.update(analyzed_names)

                    on_event(AnalysisEvent(
                        AnalysisEventType.LOG_FILE_ANALYZED,
                        f"Analyzed {len(analyzed_names)} files ({len(analysis_text)} chars)",
                        {"files": analyzed_names},
                    ))

                    findings_summary = analysis_text
                else:
                    findings_summary = "(no matching log files found)"
                    analyzed_names = [str(r) for r in requested]

                # Checkpoint
                state.save(checkpoint_path)
                on_event(AnalysisEvent(
                    AnalysisEventType.CHECKPOINT_SAVED,
                    f"Checkpoint saved (iteration {iteration})",
                    {"path": checkpoint_path},
                ))

                # Send analysis back to master
                remaining = [n for n in available_logs if n not in state.investigated_logs]
                followup = (
                    f"Analysis of {', '.join(analyzed_names)}:\n\n"
                    f"{findings_summary}\n\n"
                    f"REMAINING UNREAD LOG FILES:\n"
                    + ("\n".join(f"  - {name}" for name in remaining) if remaining else "  (none)")
                    + "\n\nEither request more logs or return your final JSON analysis."
                )

                on_event(AnalysisEvent(
                    AnalysisEventType.ITERATION_COMPLETE,
                    f"Iteration {iteration} complete. {len(remaining)} logs remaining",
                    {"iteration": iteration, "remaining_logs": len(remaining)},
                ))

                response = master.send(followup)

            else:
                response = master.send(
                    "Max iterations reached. Return your final analysis now as a JSON object."
                )
                report = self._parse_agent_response(response, topology)

        except Exception as e:
            import traceback
            logger.error("AI investigation failed: %s\n%s", e, traceback.format_exc())
            on_event(AnalysisEvent(
                AnalysisEventType.ERROR, f"Investigation failed: {e}", {"error": str(e)},
            ))
            state.save(checkpoint_path)
            report = RootCauseReport(hypothesis=f"Investigation failed: {e}")
        finally:
            master.end()

        report.iterations_used = iteration

        # Mark resolved
        state.is_resolved = True
        state.save(checkpoint_path)

        # Save artifacts
        credit_summary = modules.credit_tracker.summary()
        saved = self.save_results(report, topology, output_dir, credit_summary)
        logger.info("RCA report saved: %s", saved)

        on_event(AnalysisEvent(
            AnalysisEventType.INVESTIGATION_COMPLETE,
            f"Investigation complete: confidence {report.confidence:.0%}",
            {"confidence": report.confidence, "iterations": iteration, "output_files": saved},
        ))

        return ToolResult(
            tool_name=self.name, status="success", data=report,
            metadata={"credit_usage": credit_summary, "output_files": saved},
        )

    # --- Helpers ---

    @staticmethod
    def _parse_log_request(response: str) -> list[str]:
        import json as _json
        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                data = _json.loads(response[start:end])
                if "request_logs" in data:
                    # Normalize: agent might return strings or dicts
                    raw = data["request_logs"]
                    result = []
                    for item in raw:
                        if isinstance(item, str):
                            result.append(item)
                        elif isinstance(item, dict):
                            # Extract filename from dict like {"file": "name"} or {"filename": "name"}
                            for key in ("file", "filename", "name", "path"):
                                if key in item:
                                    result.append(str(item[key]))
                                    break
                    return result
        except (ValueError, _json.JSONDecodeError):
            pass
        return []

    @staticmethod
    def _format_topology(topology: TopologyMap) -> str:
        if not topology.nodes:
            return "(no topology available)"
        lines = ["Nodes:"]
        for arn, node in topology.nodes.items():
            lines.append(f"  - {node.name} ({node.resource_type}) [{arn}]")
        lines.append("Edges:")
        for edge in topology.edges:
            src = topology.nodes.get(edge.source_arn)
            tgt = topology.nodes.get(edge.target_arn)
            src_name = src.name if src else edge.source_arn
            tgt_name = tgt.name if tgt else edge.target_arn
            lines.append(f"  - {src_name} --[{edge.relationship}]--> {tgt_name}")
        return "\n".join(lines)

    def _parse_agent_response(self, response: str, topology: TopologyMap) -> RootCauseReport:
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
            return RootCauseReport(hypothesis=response[:500], confidence=0.3)

        remediation = data.get("suggested_remediation")
        if isinstance(remediation, list):
            remediation = "\n".join(str(r) for r in remediation)
        elif isinstance(remediation, dict):
            remediation = str(remediation)
        narrative = data.get("narrative", [])
        if isinstance(narrative, str):
            narrative = [narrative]
        elif isinstance(narrative, dict):
            narrative = [str(narrative)]
        narrative = [str(n) if not isinstance(n, str) else n for n in narrative]

        # Parse confidence — agent may return a string like "HIGH" instead of a number
        raw_confidence = data.get("confidence", 0.5)
        try:
            confidence = float(raw_confidence)
        except (ValueError, TypeError):
            # Map text labels to numbers
            label = str(raw_confidence).upper().split()[0] if raw_confidence else ""
            confidence = {"HIGH": 0.85, "MEDIUM": 0.6, "LOW": 0.3, "VERY": 0.9}.get(label, 0.5)

        return RootCauseReport(
            root_cause_chain=[str(x) for x in data.get("root_cause_chain", [])],
            confidence=confidence,
            hypothesis=str(data.get("hypothesis", "")),
            narrative=[str(n) for n in narrative],
            affected_resources=[str(x) for x in data.get("affected_resources", [])],
            suggested_remediation=remediation,
            source_evidence=data.get("source_evidence", []),
        )

    @staticmethod
    def save_report(report: RootCauseReport, output_path: str) -> str:
        import json
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report.to_dict(), indent=2, default=str))
        return str(path)

    @staticmethod
    def save_results(report: RootCauseReport, topology: TopologyMap, output_dir: str, credit_summary: dict = None) -> dict[str, str]:
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
        from datetime import datetime

        def _label(arn: str) -> str:
            node = topology.nodes.get(arn)
            return node.name if node else arn.split(":")[-1]

        def _svc(arn: str) -> str:
            node = topology.nodes.get(arn)
            return node.resource_type if node else "unknown"

        lines: list[str] = []
        lines.append("# Incident Report")
        lines.append("")
        lines.append(f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
        lines.append(f"Confidence: {report.confidence}")
        lines.append(f"Investigation depth: {report.iterations_used} iterations")
        lines.append("")

        lines.append("## Incident Timeline")
        lines.append("")
        if report.narrative:
            for entry in report.narrative:
                lines.append(str(entry))
            lines.append("")
        else:
            lines.append("No narrative could be constructed.")
            lines.append("")

        lines.append("## Root Cause Chain")
        lines.append("")
        if report.root_cause_chain:
            for i, item in enumerate(report.root_cause_chain):
                role = "ROOT CAUSE" if i == 0 else ("SURFACE" if i == len(report.root_cause_chain) - 1 else "PROPAGATION")
                lines.append(f"### {i + 1}. {_label(item)} — {role}")
                lines.append(f"- ID: `{item}`")
                lines.append("")
        else:
            lines.append("No root cause chain identified.")
            lines.append("")

        if topology.nodes:
            lines.append("## Topology Overview")
            lines.append("")
            lines.append("| Resource | Type | Health |")
            lines.append("|----------|------|--------|")
            for arn, node in topology.nodes.items():
                lines.append(f"| {node.name} | {node.resource_type} | {node.health.status} |")
            lines.append("")
            if topology.edges:
                lines.append("| Source | Relationship | Target |")
                lines.append("|--------|-------------|--------|")
                for edge in topology.edges:
                    lines.append(f"| {_label(edge.source_arn)} | {edge.relationship} | {_label(edge.target_arn)} |")
                lines.append("")

        if report.affected_resources:
            lines.append("## Affected Resources")
            lines.append("")
            for arn in report.affected_resources:
                lines.append(f"- {_label(arn)} — `{arn}`")
            lines.append("")

        lines.append("## Recommended Action")
        lines.append("")
        lines.append(str(report.suggested_remediation or "No specific remediation suggested."))
        lines.append("")

        if report.source_evidence:
            lines.append("## Source Evidence")
            lines.append("")
            for i, ev in enumerate(report.source_evidence):
                if isinstance(ev, str):
                    lines.append(f"### Evidence {i}")
                    lines.append("")
                    lines.append(ev[:500])
                    lines.append("")
                    continue
                src = ev.get("source_file", "unknown")
                excerpt = ev.get("excerpt", "")
                relevance = ev.get("relevance", "")
                lines.append(f"### Evidence {i}: `{src}`")
                lines.append("")
                if relevance:
                    lines.append(f"**Relevance:** {relevance}")
                    lines.append("")
                if excerpt:
                    lines.append("```")
                    lines.append(excerpt[:500])
                    lines.append("```")
                    lines.append("")

        if credit_summary:
            lines.append("## Credit Usage")
            lines.append("")
            lines.append(f"| Metric | Value |")
            lines.append("|--------|-------|")
            lines.append(f"| Sessions | {credit_summary.get('total_sessions', 0)} |")
            lines.append(f"| Credits | {credit_summary.get('total_credits', 0)} |")
            lines.append(f"| Time | {credit_summary.get('total_time_secs', 0)}s |")
            lines.append("")

        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines))
        return str(path)
