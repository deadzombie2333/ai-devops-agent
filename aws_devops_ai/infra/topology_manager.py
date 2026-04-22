"""Topology Manager — maintains live topology map with audit trail."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from aws_devops_ai.models import (
    ChangeSource,
    ChangeType,
    HealthStatus,
    LogFinding,
    ResourceMap,
    Severity,
    TopologyChangeRecord,
    TopologyEdge,
    TopologyMap,
    TopologyNode,
)
from aws_devops_ai.infra.resource_discoverer import _normalize_s3_arn

logger = logging.getLogger(__name__)


class TopologyManager:
    """Maintains a live topology map, records audit trail, supports export."""

    def __init__(self, topology_path: str, audit_log_path: str | None = None) -> None:
        self.topology_path = Path(topology_path)
        self.audit_log_path = Path(audit_log_path) if audit_log_path else self.topology_path.with_suffix(".audit.jsonl")
        self._topology: TopologyMap | None = None

    # ------------------------------------------------------------------
    # Load / Save
    # ------------------------------------------------------------------

    def load(self) -> TopologyMap:
        if self.topology_path.exists():
            data = json.loads(self.topology_path.read_text())
            self._topology = TopologyMap.from_dict(data)
        else:
            self._topology = TopologyMap()
        return self._topology

    def save(self) -> None:
        if self._topology is None:
            return
        self.topology_path.parent.mkdir(parents=True, exist_ok=True)
        self.topology_path.write_text(json.dumps(self._topology.to_dict(), indent=2, default=str))

    # ------------------------------------------------------------------
    # Core update
    # ------------------------------------------------------------------

    def update(
        self,
        findings: list[LogFinding],
        resource_map: ResourceMap,
        triggered_by: str = "TopologyUpdateTool",
    ) -> TopologyMap:
        """Incrementally update topology from findings and resource map."""
        topo = self.load()

        # Step 1: Merge resource map nodes
        for arn, node in resource_map.nodes.items():
            if arn not in topo.nodes:
                topo.nodes[arn] = TopologyNode(
                    arn=arn, resource_type=node.resource_type, name=node.name,
                )
                self._record_change(TopologyChangeRecord(
                    change_id=str(uuid4()),
                    timestamp=datetime.utcnow(),
                    resource_arn=arn,
                    change_type=ChangeType.NODE_ADDED,
                    source=ChangeSource.RESOURCE_DISCOVERY,
                    triggered_by=triggered_by,
                    description=f"Added node {node.name} ({node.resource_type})",
                    previous_value=None,
                    new_value={"arn": arn, "resource_type": node.resource_type, "name": node.name},
                ))
            else:
                topo.nodes[arn].metadata.update(node.properties)

        # Step 2: Merge edges (deduplicate)
        existing_keys = {(e.source_arn, e.target_arn, e.relationship) for e in topo.edges}
        for edge in resource_map.edges:
            key = (edge.source_arn, edge.target_arn, edge.relationship)
            if key not in existing_keys:
                topo.edges.append(TopologyEdge(
                    source_arn=edge.source_arn,
                    target_arn=edge.target_arn,
                    relationship=edge.relationship,
                ))
                existing_keys.add(key)
                self._record_change(TopologyChangeRecord(
                    change_id=str(uuid4()),
                    timestamp=datetime.utcnow(),
                    resource_arn=edge.source_arn,
                    change_type=ChangeType.EDGE_ADDED,
                    source=ChangeSource.RESOURCE_DISCOVERY,
                    triggered_by=triggered_by,
                    description=f"Added edge {edge.source_arn} --{edge.relationship}--> {edge.target_arn}",
                    previous_value=None,
                    new_value={"source_arn": edge.source_arn, "target_arn": edge.target_arn, "relationship": edge.relationship},
                ))

        # Step 3: Update health from findings
        for finding in findings:
            for raw_arn in finding.resource_arns:
                arn = _normalize_s3_arn(raw_arn)
                if arn not in topo.nodes:
                    continue
                node = topo.nodes[arn]
                prev_status = node.health.status

                if finding.severity in (Severity.ERROR, Severity.CRITICAL):
                    node.health.status = "error"
                    node.health.last_error = finding.message
                    node.health.error_count += 1
                    node.health.last_seen = finding.timestamp
                    if prev_status != "error":
                        self._record_change(TopologyChangeRecord(
                            change_id=str(uuid4()),
                            timestamp=datetime.utcnow(),
                            resource_arn=arn,
                            change_type=ChangeType.HEALTH_STATUS_CHANGED,
                            source=ChangeSource.LOG_ANALYSIS,
                            triggered_by=triggered_by,
                            description=f"Health changed from '{prev_status}' to 'error': {finding.message}",
                            previous_value={"status": prev_status},
                            new_value={"status": "error", "last_error": finding.message},
                        ))
                    else:
                        self._record_change(TopologyChangeRecord(
                            change_id=str(uuid4()),
                            timestamp=datetime.utcnow(),
                            resource_arn=arn,
                            change_type=ChangeType.HEALTH_ERROR_RECORDED,
                            source=ChangeSource.LOG_ANALYSIS,
                            triggered_by=triggered_by,
                            description=f"Additional error recorded: {finding.message}",
                            previous_value={"error_count": node.health.error_count - 1},
                            new_value={"error_count": node.health.error_count},
                        ))

                elif finding.severity == Severity.WARNING and node.health.status != "error":
                    node.health.last_seen = finding.timestamp
                    if prev_status != "warning":
                        node.health.status = "warning"
                        self._record_change(TopologyChangeRecord(
                            change_id=str(uuid4()),
                            timestamp=datetime.utcnow(),
                            resource_arn=arn,
                            change_type=ChangeType.HEALTH_STATUS_CHANGED,
                            source=ChangeSource.LOG_ANALYSIS,
                            triggered_by=triggered_by,
                            description=f"Health changed from '{prev_status}' to 'warning'",
                            previous_value={"status": prev_status},
                            new_value={"status": "warning"},
                        ))

        topo.last_updated = datetime.utcnow()
        topo.version += 1
        self._topology = topo
        self.save()
        return topo

    # ------------------------------------------------------------------
    # Health summary
    # ------------------------------------------------------------------

    def get_health_summary(self) -> dict[str, HealthStatus]:
        topo = self._topology or self.load()
        return {arn: node.health for arn, node in topo.nodes.items()}

    # ------------------------------------------------------------------
    # Audit trail
    # ------------------------------------------------------------------

    def _record_change(self, record: TopologyChangeRecord) -> None:
        """Append a change record to the audit log (JSONL, append-only)."""
        self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.audit_log_path, "a") as f:
            f.write(json.dumps(record.to_dict(), default=str) + "\n")

    # ------------------------------------------------------------------
    # Human updates
    # ------------------------------------------------------------------

    def apply_human_update(self, operator_id: str, updates: list) -> TopologyMap:
        """Apply human-provided topology updates with audit trail."""
        from aws_devops_ai.models import HumanTopologyUpdate
        from dataclasses import asdict

        topo = self.load()
        triggered_by = f"operator:{operator_id}"

        for update in updates:
            arn = update.resource_arn

            if update.action == "add_node":
                if arn in topo.nodes:
                    logger.warning("Node %s already exists, skipping add_node", arn)
                    continue
                topo.nodes[arn] = TopologyNode(
                    arn=arn,
                    resource_type=update.data["resource_type"],
                    name=update.data["name"],
                    metadata=update.data.get("metadata", {}),
                )
                self._record_change(TopologyChangeRecord(
                    change_id=str(uuid4()), timestamp=datetime.utcnow(),
                    resource_arn=arn, change_type=ChangeType.NODE_ADDED,
                    source=ChangeSource.HUMAN_INPUT, triggered_by=triggered_by,
                    description=f"Human added node {update.data['name']} ({update.data['resource_type']})",
                    previous_value=None, new_value=update.data,
                ))

            elif update.action == "remove_node":
                if arn not in topo.nodes:
                    logger.warning("Node %s not found, skipping remove_node", arn)
                    continue
                prev = topo.nodes[arn].to_dict()
                del topo.nodes[arn]
                topo.edges = [e for e in topo.edges if e.source_arn != arn and e.target_arn != arn]
                self._record_change(TopologyChangeRecord(
                    change_id=str(uuid4()), timestamp=datetime.utcnow(),
                    resource_arn=arn, change_type=ChangeType.NODE_REMOVED,
                    source=ChangeSource.HUMAN_INPUT, triggered_by=triggered_by,
                    description=f"Human removed node {arn}",
                    previous_value=prev, new_value=None,
                ))

            elif update.action == "add_edge":
                target_arn = update.data["target_arn"]
                relationship = update.data["relationship"]
                existing_keys = {(e.source_arn, e.target_arn, e.relationship) for e in topo.edges}
                if (arn, target_arn, relationship) in existing_keys:
                    logger.warning("Edge (%s, %s, %s) already exists, skipping", arn, target_arn, relationship)
                    continue
                topo.edges.append(TopologyEdge(source_arn=arn, target_arn=target_arn, relationship=relationship))
                self._record_change(TopologyChangeRecord(
                    change_id=str(uuid4()), timestamp=datetime.utcnow(),
                    resource_arn=arn, change_type=ChangeType.EDGE_ADDED,
                    source=ChangeSource.HUMAN_INPUT, triggered_by=triggered_by,
                    description=f"Human added edge {arn} --{relationship}--> {target_arn}",
                    previous_value=None,
                    new_value={"source_arn": arn, "target_arn": target_arn, "relationship": relationship},
                ))

            elif update.action == "remove_edge":
                target_arn = update.data["target_arn"]
                relationship = update.data["relationship"]
                before_count = len(topo.edges)
                topo.edges = [e for e in topo.edges if not (e.source_arn == arn and e.target_arn == target_arn and e.relationship == relationship)]
                if len(topo.edges) < before_count:
                    self._record_change(TopologyChangeRecord(
                        change_id=str(uuid4()), timestamp=datetime.utcnow(),
                        resource_arn=arn, change_type=ChangeType.EDGE_REMOVED,
                        source=ChangeSource.HUMAN_INPUT, triggered_by=triggered_by,
                        description=f"Human removed edge {arn} --{relationship}--> {target_arn}",
                        previous_value={"source_arn": arn, "target_arn": target_arn, "relationship": relationship},
                        new_value=None,
                    ))

            elif update.action == "update_health":
                if arn not in topo.nodes:
                    logger.warning("Node %s not found, skipping update_health", arn)
                    continue
                node = topo.nodes[arn]
                prev_status = node.health.status
                node.health.status = update.data["status"]
                if "last_error" in update.data:
                    node.health.last_error = update.data["last_error"]
                self._record_change(TopologyChangeRecord(
                    change_id=str(uuid4()), timestamp=datetime.utcnow(),
                    resource_arn=arn, change_type=ChangeType.HEALTH_STATUS_CHANGED,
                    source=ChangeSource.HUMAN_INPUT, triggered_by=triggered_by,
                    description=f"Human changed health from '{prev_status}' to '{update.data['status']}'",
                    previous_value={"status": prev_status},
                    new_value={"status": update.data["status"]},
                ))

            elif update.action == "annotate":
                if arn not in topo.nodes:
                    logger.warning("Node %s not found, skipping annotate", arn)
                    continue
                topo.nodes[arn].metadata["human_annotation"] = update.data["text"]
                self._record_change(TopologyChangeRecord(
                    change_id=str(uuid4()), timestamp=datetime.utcnow(),
                    resource_arn=arn, change_type=ChangeType.HUMAN_ANNOTATION,
                    source=ChangeSource.HUMAN_INPUT, triggered_by=triggered_by,
                    description=f"Human annotation: {update.data['text']}",
                    previous_value=None,
                    new_value={"annotation": update.data["text"]},
                ))

        topo.last_updated = datetime.utcnow()
        topo.version += 1
        self._topology = topo
        self.save()
        return topo

    # ------------------------------------------------------------------
    # Audit trail query
    # ------------------------------------------------------------------

    def get_change_history(
        self,
        resource_arn: str | None = None,
        since: datetime | None = None,
        change_type: ChangeType | None = None,
        source: ChangeSource | None = None,
    ) -> list[TopologyChangeRecord]:
        """Query audit trail with optional AND-logic filters."""
        if not self.audit_log_path.exists():
            return []

        records: list[TopologyChangeRecord] = []
        with open(self.audit_log_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = TopologyChangeRecord.from_dict(json.loads(line))

                if resource_arn and record.resource_arn != resource_arn:
                    continue
                if since and record.timestamp < since:
                    continue
                if change_type and record.change_type != change_type:
                    continue
                if source and record.source != source:
                    continue

                records.append(record)

        records.sort(key=lambda r: r.timestamp)
        return records

    # ------------------------------------------------------------------
    # Export — DOT (Graphviz)
    # ------------------------------------------------------------------

    def export_dot(self, output_path: str | None = None) -> str:
        """Export topology to DOT format. Returns DOT string, optionally writes to file."""
        topo = self._topology or self.load()
        # Service-type colors for visual grouping (structural, not health)
        svc_colors = {
            "lambda": "lightyellow", "apigateway": "lightcyan", "rds": "lightblue",
            "dynamodb": "lightsalmon", "s3": "lightgreen", "sqs": "plum",
            "sns": "peachpuff", "ec2": "wheat", "iam": "lavender",
        }

        lines = ["digraph topology {", "    rankdir=LR;", '    node [shape=box, style=filled];', ""]

        for arn, node in sorted(topo.nodes.items()):
            svc = node.resource_type.split(":")[0]
            color = svc_colors.get(svc, "white")
            label = f"{node.name}\\n{node.resource_type}"
            safe_id = arn.replace(":", "_").replace("/", "_")
            lines.append(f'    "{safe_id}" [label="{label}", fillcolor={color}];')

        lines.append("")

        for edge in topo.edges:
            src = edge.source_arn.replace(":", "_").replace("/", "_")
            tgt = edge.target_arn.replace(":", "_").replace("/", "_")
            lines.append(f'    "{src}" -> "{tgt}" [label="{edge.relationship}"];')

        lines.append("}")
        dot_str = "\n".join(lines)

        if output_path:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_text(dot_str)

        return dot_str

    # ------------------------------------------------------------------
    # Export — GraphML (XML)
    # ------------------------------------------------------------------

    def export_graphml(self, output_path: str | None = None) -> str:
        """Export topology to GraphML format. Returns XML string, optionally writes to file."""
        topo = self._topology or self.load()

        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<graphml xmlns="http://graphml.graphstruct.org/graphml"',
            '         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
            '  <key id="arn" for="node" attr.name="arn" attr.type="string"/>',
            '  <key id="resource_type" for="node" attr.name="resource_type" attr.type="string"/>',
            '  <key id="name" for="node" attr.name="name" attr.type="string"/>',
            '  <key id="relationship" for="edge" attr.name="relationship" attr.type="string"/>',
            '  <key id="request_count" for="edge" attr.name="request_count" attr.type="int"/>',
            '  <key id="error_count" for="edge" attr.name="error_count" attr.type="int"/>',
            '  <graph id="topology" edgedefault="directed">',
        ]

        for arn, node in sorted(topo.nodes.items()):
            safe_id = arn.replace(":", "_").replace("/", "_")
            lines.append(f'    <node id="{safe_id}">')
            lines.append(f'      <data key="arn">{arn}</data>')
            lines.append(f'      <data key="resource_type">{node.resource_type}</data>')
            lines.append(f'      <data key="name">{node.name}</data>')
            lines.append('    </node>')

        for i, edge in enumerate(topo.edges):
            src = edge.source_arn.replace(":", "_").replace("/", "_")
            tgt = edge.target_arn.replace(":", "_").replace("/", "_")
            lines.append(f'    <edge id="e{i}" source="{src}" target="{tgt}">')
            lines.append(f'      <data key="relationship">{edge.relationship}</data>')
            lines.append(f'      <data key="request_count">{edge.request_count}</data>')
            lines.append(f'      <data key="error_count">{edge.error_count}</data>')
            lines.append('    </edge>')

        lines.append('  </graph>')
        lines.append('</graphml>')
        graphml_str = "\n".join(lines)

        if output_path:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_text(graphml_str)

        return graphml_str


    def save_all_artifacts(self, output_dir: str) -> dict[str, str]:
        """Export topology artifacts (JSON, DOT, PNG, audit log) to a directory."""
        import shutil
        import subprocess

        Path(output_dir).mkdir(parents=True, exist_ok=True)
        paths: dict[str, str] = {}

        # topology.json — structured data for agent and future updates
        json_dest = str(Path(output_dir) / "topology.json")
        if str(Path(self.topology_path).resolve()) != str(Path(json_dest).resolve()):
            shutil.copy2(self.topology_path, json_dest)
        paths["json"] = json_dest

        # topology.dot — editable source
        dot_path = str(Path(output_dir) / "topology.dot")
        self.export_dot(dot_path)
        paths["dot"] = dot_path

        # topology.png — human visualization
        png_path = str(Path(output_dir) / "topology.png")
        try:
            subprocess.run(["dot", "-Tpng", dot_path, "-o", png_path], check=True, timeout=30)
            paths["png"] = png_path
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass

        # topology_audit.jsonl — change tracking
        if self.audit_log_path and Path(self.audit_log_path).exists():
            audit_dest = str(Path(output_dir) / "topology_audit.jsonl")
            if str(Path(self.audit_log_path).resolve()) != str(Path(audit_dest).resolve()):
                shutil.copy2(self.audit_log_path, audit_dest)
            paths["audit"] = audit_dest

        return paths

    def print_summary(self) -> None:
        """Print a human-readable topology summary to stdout."""
        topo = self._topology or self.load()

        print(f"\nTopology: {len(topo.nodes)} nodes, {len(topo.edges)} edges")

        print("\n--- Resources ---")
        for arn, node in topo.nodes.items():
            print(f"  {node.name:30s} {node.resource_type:20s} {arn}")

        print("\n--- Connections ---")
        for edge in topo.edges:
            src = topo.nodes.get(edge.source_arn)
            tgt = topo.nodes.get(edge.target_arn)
            src_label = src.name if src else edge.source_arn.split(":")[-1]
            tgt_label = tgt.name if tgt else edge.target_arn.split(":")[-1]
            print(f"  {src_label} --[{edge.relationship}]--> {tgt_label}")

