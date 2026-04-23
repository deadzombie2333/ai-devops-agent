"""TopologyUpdateTool — builds topology from log analysis text."""

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
    }

    _FINDINGS_BATCH_LIMIT = 80_000

    def execute(self, params: dict, modules: ModuleRegistry) -> ToolResult:
        from aws_devops_ai.infra.ask_claude import ClaudeSession
        from aws_devops_ai.infra.file_readers import is_supported_file
        import json as _json
        from aws_devops_ai.models import ResourceMap, TopologyMap

        analyzer = modules.log_analyzer_agent
        topo_mgr = modules.topology_manager

        # Step 1: Collect log files
        log_dir = params.get("log_dir")
        if not log_dir:
            sources = params.get("sources", modules.config.sources)
            log_dir = sources[0].identifier if sources else modules.config.log_dir

        log_dir_path = Path(log_dir)
        log_paths = sorted(
            f for f in log_dir_path.rglob("*")
            if f.is_file() and is_supported_file(f) and not f.name.startswith(".")
        ) if log_dir_path.is_dir() else []

        # Step 2: Let CC analyze all files — returns plain text
        analysis_text = analyzer.analyze(log_paths) if log_paths else ""

        if not analysis_text.strip():
            logger.warning("No analysis results from log files")
            return ToolResult(
                tool_name=self.name, status="success",
                data=TopologyUpdateResult(new_logs_downloaded=len(log_paths)),
            )

        # Step 3: Split analysis text into batches if too large
        batches = self._split_text(analysis_text, self._FINDINGS_BATCH_LIMIT)

        # Step 4: Build topology incrementally
        topology = topo_mgr.load()

        for i, batch_text in enumerate(batches):
            topo_json = _json.dumps(topology.to_dict(), indent=2, default=str) if topology.nodes else "{}"

            if i == 0 and not topology.nodes:
                prompt = self._initial_prompt(batch_text)
            else:
                prompt = self._incremental_prompt(topo_json, batch_text)

            session = ClaudeSession(
                model=modules.config.mid_resource_model,
                credit_tracker=modules.credit_tracker,
                label=f"topology-builder:batch-{i+1}",
            )
            try:
                response = session.start(prompt)
                topology = self._parse_topology_response(response)
            except Exception as e:
                logger.error("AI topology inference failed on batch %d: %s", i + 1, e)
            finally:
                session.end()

            logger.info("Batch %d/%d: %d nodes, %d edges",
                        i + 1, len(batches), len(topology.nodes), len(topology.edges))

        # Step 5: Save
        topo_mgr._topology = topology
        topo_mgr.save()
        topo_mgr.save_all_artifacts(modules.config.topology_output_dir)

        return ToolResult(
            tool_name=self.name,
            status="success",
            data=TopologyUpdateResult(
                findings=[],
                resource_map=ResourceMap(),
                topology=topology,
                new_logs_downloaded=len(log_paths),
                purged_count=0,
            ),
        )

    @staticmethod
    def _split_text(text: str, max_size: int) -> list[str]:
        """Split text into chunks that fit within max_size chars."""
        if len(text) <= max_size:
            return [text]
        chunks = []
        while text:
            chunks.append(text[:max_size])
            text = text[max_size:]
        return chunks

    @staticmethod
    def _initial_prompt(analysis_text: str) -> str:
        return f"""You are a senior cloud architect. Based on the following log analysis,
build a service topology map identifying all resources and their connections.

LOG ANALYSIS:
{analysis_text}

Rules:
- Each node needs a unique resource_id (ARN, hostname, service name, or descriptive URI),
  a short human-readable name, and a resource_type
- Edges: depends_on, reads_from, writes_to, invokes, routes_to, etc.
- Edge direction: source is the caller, target is the dependency
- Purely structural — how services are wired, NOT about health

Return a JSON object with:
- "nodes": list of {{"resource_id": "...", "name": "...", "resource_type": "..."}}
- "edges": list of {{"source_id": "...", "target_id": "...", "relationship": "..."}}

Return ONLY the JSON object."""

    @staticmethod
    def _incremental_prompt(current_topology_json: str, analysis_text: str) -> str:
        return f"""You are updating an existing service topology with new log analysis.

CURRENT TOPOLOGY:
{current_topology_json}

NEW LOG ANALYSIS:
{analysis_text}

Rules:
- Add any new nodes and edges discovered
- Keep all existing nodes and edges — do not remove anything
- Merge duplicates (same resource_id = same node)
- Purely structural — no health status

Return the COMPLETE updated topology as a JSON object with:
- "nodes": list of {{"resource_id": "...", "name": "...", "resource_type": "..."}}
- "edges": list of {{"source_id": "...", "target_id": "...", "relationship": "..."}}

Return ONLY the JSON object."""

    @staticmethod
    def _parse_topology_response(response: str):
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
