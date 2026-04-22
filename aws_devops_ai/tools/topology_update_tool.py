"""TopologyUpdateTool — high-resource agent for log download, analysis, and topology update."""

from __future__ import annotations

import logging
from pathlib import Path

from aws_devops_ai.models import LogSource, ToolResult, TopologyUpdateResult
from aws_devops_ai.tool_registry import DevOpsTool, ModuleRegistry

logger = logging.getLogger(__name__)


class TopologyUpdateTool(DevOpsTool):
    """Read logs from a local folder, analyze them, and build/update topology."""

    name = "topology_update"
    description = "Read logs from a local folder, analyze them, and build/update topology"
    parameters = {
        "log_dir": "str — path to local log folder to read",
        "force_reanalysis": "bool — re-analyze even if topology exists (default: False)",
    }

    # Max chars of findings text per batch sent to the topology agent
    _FINDINGS_BATCH_LIMIT = 80_000

    def execute(self, params: dict, modules: ModuleRegistry) -> ToolResult:
        from aws_devops_ai.infra.ask_kiro import KiroSession
        from aws_devops_ai.infra.file_readers import SUPPORTED_EXTENSIONS, is_supported_file
        import json as _json

        from aws_devops_ai.models import (
            ResourceMap,
            TopologyMap,
        )

        analyzer = modules.log_analyzer_agent
        topo_mgr = modules.topology_manager

        # Step 1: Collect and analyze log files
        log_dir = params.get("log_dir")
        if not log_dir:
            sources = params.get("sources", modules.config.sources)
            log_dir = sources[0].location if sources else modules.config.log_dir

        log_dir_path = Path(log_dir)
        log_paths = sorted(
            f for f in log_dir_path.rglob("*")
            if f.is_file() and is_supported_file(f) and not f.name.startswith(".")
        ) if log_dir_path.is_dir() else []
        all_findings = analyzer.analyze(log_paths) if log_paths else []

        # Step 2: Deduplicate findings
        deduped = []
        seen = set()
        for f in all_findings:
            key = (f.message[:80], tuple(f.resource_arns))
            if key not in seen:
                seen.add(key)
                deduped.append(f)

        # Step 3: Split findings into batches that fit the context limit
        batches: list[list] = []
        current_batch: list = []
        current_size = 0
        for f in deduped:
            ts = f.timestamp.strftime("%H:%M:%S") if f.timestamp else "??:??:??"
            arns = ", ".join(f.resource_arns) if f.resource_arns else "(no ARN)"
            entry = f"[{ts}] [{f.severity.value.upper()}] {f.message}\n  ARNs: {arns} | Source: {f.source_file}\n"
            entry_size = len(entry)
            if current_size + entry_size > self._FINDINGS_BATCH_LIMIT and current_batch:
                batches.append(current_batch)
                current_batch = []
                current_size = 0
            current_batch.append(entry)
            current_size += entry_size
        if current_batch:
            batches.append(current_batch)

        # Step 4: Build topology incrementally — one batch at a time
        topology = topo_mgr.load()

        for i, batch in enumerate(batches):
            findings_block = "".join(batch)
            topo_json = _json.dumps(topology.to_dict(), indent=2, default=str) if topology.nodes else "{}"

            if i == 0 and not topology.nodes:
                prompt = self._initial_prompt(findings_block)
            else:
                prompt = self._incremental_prompt(topo_json, findings_block)

            session = KiroSession(model=modules.config.high_resource_model, timeout=180, credit_tracker=modules.credit_tracker, label=f"topology-builder:batch-{i+1}")
            try:
                response = session.start(prompt)
                topology = self._parse_topology_response(response)
            except Exception as e:
                logger.error("AI topology inference failed on batch %d: %s", i + 1, e)
            finally:
                session.end()

            logger.info("Batch %d/%d: %d nodes, %d edges", i + 1, len(batches), len(topology.nodes), len(topology.edges))

        # Step 5: Save
        topo_mgr._topology = topology
        topo_mgr.save()
        topo_mgr.save_all_artifacts(modules.config.topology_output_dir)

        return ToolResult(
            tool_name=self.name,
            status="success",
            data=TopologyUpdateResult(
                findings=all_findings,
                resource_map=ResourceMap(),
                topology=topology,
                new_logs_downloaded=len(log_paths),
                purged_count=0,
            ),
        )

    @staticmethod
    def _initial_prompt(findings_block: str) -> str:
        return f"""You are a senior cloud architect analyzing log findings to build a service topology map.

Identify all cloud resources (nodes) and how they connect (edges).
This is purely structural — how services are wired, NOT about health or errors.
Logs may come from any cloud provider (AWS, GCP, Azure) or on-prem services.

LOG FINDINGS:
{findings_block}

Rules:
- Each node needs a unique resource_id (ARN for AWS, resource path for GCP/Azure, or a descriptive URI for on-prem), a short human-readable name, a resource_type, and a cloud provider name
- Edges: depends_on, reads_from, writes_to, invokes, routes_to, replicates_to, etc.
- Edge direction: source is the caller, target is the dependency
- Infer connections from HTTP endpoints, service names, timing, and context
- Do NOT include health status

Return a JSON object with:
- "nodes": list of {{"resource_id": "...", "name": "...", "resource_type": "...", "provider": "aws|gcp|azure|on-prem"}}
- "edges": list of {{"source_id": "...", "target_id": "...", "relationship": "..."}}

Return ONLY the JSON object."""

    @staticmethod
    def _incremental_prompt(current_topology_json: str, findings_block: str) -> str:
        return f"""You are updating an existing service topology with new log findings.
Logs may come from any cloud provider (AWS, GCP, Azure) or on-prem services.

CURRENT TOPOLOGY:
{current_topology_json}

NEW LOG FINDINGS:
{findings_block}

Rules:
- Add any new nodes and edges discovered in the new findings
- Keep all existing nodes and edges — do not remove anything
- Merge duplicates (same resource_id = same node)
- Infer connections from HTTP endpoints, service names, timing, and context
- Purely structural — no health status

Return the COMPLETE updated topology as a JSON object with:
- "nodes": list of {{"resource_id": "...", "name": "...", "resource_type": "...", "provider": "aws|gcp|azure|on-prem"}}
- "edges": list of {{"source_id": "...", "target_id": "...", "relationship": "..."}}

Return ONLY the JSON object."""

    @staticmethod
    def _parse_topology_response(response: str):
        """Parse AI response into a TopologyMap."""
        import json as _json
        from aws_devops_ai.models import TopologyEdge, TopologyMap, TopologyNode

        try:
            start = response.find("{")
            end = response.rfind("}") + 1
            if start >= 0 and end > start:
                data = _json.loads(response[start:end])
            else:
                raise ValueError("No JSON object found")
        except (ValueError, _json.JSONDecodeError) as e:
            logger.warning("Failed to parse topology response: %s", e)
            return TopologyMap()

        topo = TopologyMap()
        for n in data.get("nodes", []):
            rid = n.get("resource_id") or n.get("arn", "")
            if rid:
                topo.nodes[rid] = TopologyNode(
                    arn=rid,
                    resource_type=n.get("resource_type", "unknown"),
                    name=n.get("name", rid.split(":")[-1]),
                )
        for e in data.get("edges", []):
            src = e.get("source_id") or e.get("source_arn", "")
            tgt = e.get("target_id") or e.get("target_arn", "")
            rel = e.get("relationship", "connected_to")
            if src and tgt:
                topo.edges.append(TopologyEdge(source_arn=src, target_arn=tgt, relationship=rel))

        return topo


    @staticmethod
    def print_summary(update: TopologyUpdateResult) -> None:
        """Print topology update summary to stdout."""
        print(f"\nLogs analyzed: {update.new_logs_downloaded}")
        print(f"Findings extracted: {len(update.findings)}")

        if update.findings:
            print("\n--- Findings ---")
            for f in update.findings:
                print(f"  [{f.severity.value.upper():8s}] {f.message[:100]}")
                for arn in f.resource_arns[:3]:
                    print(f"             -> {arn}")

