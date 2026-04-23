"""Data models, enums, and validation for AWS DevOps AI."""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class LogSourceType(Enum):
    CLOUDWATCH = "cloudwatch"
    CLOUDTRAIL = "cloudtrail"
    S3_BUCKET = "s3_bucket"
    LOCAL_FILE = "local_file"


class Severity(Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ChangeType(Enum):
    NODE_ADDED = "node_added"
    NODE_REMOVED = "node_removed"
    NODE_METADATA_UPDATED = "node_metadata_updated"
    EDGE_ADDED = "edge_added"
    EDGE_REMOVED = "edge_removed"
    HEALTH_STATUS_CHANGED = "health_status_changed"
    HEALTH_ERROR_RECORDED = "health_error_recorded"
    HUMAN_ANNOTATION = "human_annotation"


class ChangeSource(Enum):
    LOG_ANALYSIS = "log_analysis"
    RESOURCE_DISCOVERY = "resource_discovery"
    HUMAN_INPUT = "human_input"
    TOPOLOGY_MERGE = "topology_merge"
    STALE_PRUNE = "stale_prune"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_AWS_REGION_RE = re.compile(
    r"^(us|eu|ap|sa|ca|me|af|il|mx)-(north|south|east|west|central|northeast|southeast|northwest|southwest)-\d+$"
)


def _validate_aws_region(region: str) -> None:
    if not _AWS_REGION_RE.match(region):
        raise ValueError(f"Invalid AWS region: {region!r}")


def _validate_non_empty(value: str, name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{name} must be non-empty")


def _validate_uuid(value: str, name: str) -> None:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ValueError(f"{name} must be a valid UUID, got {value!r}")


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class LogNotFoundError(Exception):
    """Raised when a log no longer exists at the AWS source."""
    pass


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class LogSource:
    source_type: LogSourceType
    identifier: str  # log group name, trail name, bucket name, or local dir
    region: str = "us-east-1"
    prefix: str | None = None  # S3 key prefix or local glob filter

    def __post_init__(self):
        _validate_non_empty(self.identifier, "identifier")
        if self.source_type != LogSourceType.LOCAL_FILE:
            _validate_aws_region(self.region)


@dataclass
class LogReference:
    source: LogSource
    key: str  # unique key within source
    timestamp: datetime
    size_bytes: int = 0

    @property
    def unique_id(self) -> str:
        return f"{self.source.source_type.value}:{self.source.identifier}:{self.key}"


@dataclass
class DownloadRecord:
    unique_id: str
    source_type: str
    source_identifier: str
    key: str
    local_path: str | None
    downloaded_at: datetime
    is_purged: bool = False
    purged_at: datetime | None = None

    def validate(self) -> None:
        """Full validation including filesystem checks. Call explicitly when needed."""
        _validate_non_empty(self.unique_id, "unique_id")
        if self.is_purged:
            if self.local_path is not None:
                raise ValueError("local_path must be None when is_purged is True")
            if self.purged_at is None:
                raise ValueError("purged_at must be set when is_purged is True")


@dataclass
class LogFinding:
    source_file: str
    timestamp: datetime
    severity: Severity
    message: str
    resource_arns: list[str] = field(default_factory=list)
    raw_lines: list[str] = field(default_factory=list)
    line_numbers: list[int] = field(default_factory=list)
    context: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "source_file": self.source_file,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "severity": self.severity.value,
            "message": self.message,
            "resource_arns": self.resource_arns,
            "raw_lines": self.raw_lines,
            "line_numbers": self.line_numbers,
            "context": self.context,
        }


# ---------------------------------------------------------------------------
# Resource map models
# ---------------------------------------------------------------------------

@dataclass
class ResourceNode:
    arn: str
    resource_type: str
    name: str
    region: str = "us-east-1"
    properties: dict = field(default_factory=dict)


@dataclass
class ResourceEdge:
    source_arn: str
    target_arn: str
    relationship: str


@dataclass
class ResourceMap:
    nodes: dict[str, ResourceNode] = field(default_factory=dict)
    edges: list[ResourceEdge] = field(default_factory=list)
    last_updated: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "nodes": {
                arn: {
                    "arn": n.arn,
                    "resource_type": n.resource_type,
                    "name": n.name,
                    "region": n.region,
                    "properties": n.properties,
                }
                for arn, n in self.nodes.items()
            },
            "edges": [
                {
                    "source_arn": e.source_arn,
                    "target_arn": e.target_arn,
                    "relationship": e.relationship,
                }
                for e in self.edges
            ],
            "last_updated": self.last_updated.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> ResourceMap:
        nodes = {}
        for arn, nd in data.get("nodes", {}).items():
            nodes[arn] = ResourceNode(**nd)
        edges = [ResourceEdge(**ed) for ed in data.get("edges", [])]
        last_updated = datetime.fromisoformat(data["last_updated"]) if "last_updated" in data else datetime.utcnow()
        return cls(nodes=nodes, edges=edges, last_updated=last_updated)


# ---------------------------------------------------------------------------
# Topology change record
# ---------------------------------------------------------------------------

@dataclass
class TopologyChangeRecord:
    change_id: str
    timestamp: datetime
    resource_arn: str
    change_type: ChangeType
    source: ChangeSource
    triggered_by: str
    description: str
    previous_value: dict | None = None
    new_value: dict | None = None

    def __post_init__(self):
        _validate_uuid(self.change_id, "change_id")
        _validate_non_empty(self.resource_arn, "resource_arn")
        _validate_non_empty(self.triggered_by, "triggered_by")

    def to_dict(self) -> dict:
        return {
            "change_id": self.change_id,
            "timestamp": self.timestamp.isoformat(),
            "resource_arn": self.resource_arn,
            "change_type": self.change_type.value,
            "source": self.source.value,
            "triggered_by": self.triggered_by,
            "description": self.description,
            "previous_value": self.previous_value,
            "new_value": self.new_value,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TopologyChangeRecord:
        return cls(
            change_id=data["change_id"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            resource_arn=data["resource_arn"],
            change_type=ChangeType(data["change_type"]),
            source=ChangeSource(data["source"]),
            triggered_by=data["triggered_by"],
            description=data["description"],
            previous_value=data.get("previous_value"),
            new_value=data.get("new_value"),
        )


# ---------------------------------------------------------------------------
# Topology map models
# ---------------------------------------------------------------------------

@dataclass
class HealthStatus:
    status: str = "unknown"  # "healthy", "warning", "error", "unknown"
    last_error: str | None = None
    error_count: int = 0
    last_seen: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "last_error": self.last_error,
            "error_count": self.error_count,
            "last_seen": self.last_seen.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> HealthStatus:
        return cls(
            status=data.get("status", "unknown"),
            last_error=data.get("last_error"),
            error_count=data.get("error_count", 0),
            last_seen=datetime.fromisoformat(data["last_seen"]) if "last_seen" in data else datetime.utcnow(),
        )


@dataclass
class TopologyNode:
    arn: str
    resource_type: str
    name: str
    health: HealthStatus = field(default_factory=HealthStatus)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "arn": self.arn,
            "resource_type": self.resource_type,
            "name": self.name,
            "health": self.health.to_dict(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TopologyNode:
        return cls(
            arn=data["arn"],
            resource_type=data["resource_type"],
            name=data["name"],
            health=HealthStatus.from_dict(data["health"]) if "health" in data else HealthStatus(),
            metadata=data.get("metadata", {}),
        )


@dataclass
class TopologyEdge:
    source_arn: str
    target_arn: str
    relationship: str
    request_count: int = 0
    error_count: int = 0

    def to_dict(self) -> dict:
        return {
            "source_arn": self.source_arn,
            "target_arn": self.target_arn,
            "relationship": self.relationship,
            "request_count": self.request_count,
            "error_count": self.error_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TopologyEdge:
        return cls(**data)


@dataclass
class TopologyMap:
    nodes: dict[str, TopologyNode] = field(default_factory=dict)
    edges: list[TopologyEdge] = field(default_factory=list)
    generated_at: datetime = field(default_factory=datetime.utcnow)
    last_updated: datetime = field(default_factory=datetime.utcnow)
    version: int = 1

    def to_dict(self) -> dict:
        return {
            "nodes": {arn: n.to_dict() for arn, n in self.nodes.items()},
            "edges": [e.to_dict() for e in self.edges],
            "generated_at": self.generated_at.isoformat(),
            "last_updated": self.last_updated.isoformat(),
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TopologyMap:
        nodes = {arn: TopologyNode.from_dict(nd) for arn, nd in data.get("nodes", {}).items()}
        edges = [TopologyEdge.from_dict(ed) for ed in data.get("edges", [])]
        return cls(
            nodes=nodes,
            edges=edges,
            generated_at=datetime.fromisoformat(data["generated_at"]) if "generated_at" in data else datetime.utcnow(),
            last_updated=datetime.fromisoformat(data["last_updated"]) if "last_updated" in data else datetime.utcnow(),
            version=data.get("version", 1),
        )


# ---------------------------------------------------------------------------
# Human topology update
# ---------------------------------------------------------------------------

@dataclass
class HumanTopologyUpdate:
    action: str  # "add_node", "remove_node", "add_edge", "remove_edge", "update_health", "annotate"
    resource_arn: str
    data: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class SystemConfig:
    log_dir: str = "./logs"
    tracker_db: str = "./tracking.db"
    resource_map_path: str = "./resource_map.json"
    topology_path: str = "./topology.json"
    topology_audit_log_path: str = "./topology_audit.jsonl"
    topology_output_dir: str = "./topology_output"
    rca_output_dir: str = "./rca_output"
    high_resource_model: str = ""   # top: planning, RCA master
    mid_resource_model: str = ""    # mid: analysis, topology inference
    low_resource_model: str = ""    # bottom: file reading, extraction

    def __post_init__(self):
        import os
        if not self.high_resource_model:
            self.high_resource_model = os.environ.get("ANTHROPIC_DEFAULT_OPUS_MODEL", "claude-opus-4-6")
        if not self.mid_resource_model:
            self.mid_resource_model = os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-6")
        if not self.low_resource_model:
            self.low_resource_model = os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", "claude-haiku-4-5")
    max_concurrent_analyzers: int = 3
    log_batch_size: int = 10
    retention_days: int = 7
    max_investigation_iterations: int = 10
    sources: list[LogSource] = field(default_factory=list)


@dataclass
class TriggerContext:
    trigger_type: str  # "manual", "s3_event", "cloudwatch_event"
    event_data: dict = field(default_factory=dict)
    target_arn: str | None = None
    error_pattern: str | None = None
    timestamp: datetime = field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Tool result models
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    tool_name: str
    status: str  # "success", "partial", "error"
    data: Any = None
    metadata: dict = field(default_factory=dict)


@dataclass
class TopologyUpdateResult:
    findings: list[LogFinding] = field(default_factory=list)
    resource_map: ResourceMap = field(default_factory=ResourceMap)
    topology: TopologyMap = field(default_factory=TopologyMap)
    new_logs_downloaded: int = 0
    purged_count: int = 0


@dataclass
class RootCauseReport:
    root_cause_chain: list[str] = field(default_factory=list)
    confidence: float = 0.0
    hypothesis: str = ""
    supporting_findings: list[LogFinding] = field(default_factory=list)
    affected_resources: list[str] = field(default_factory=list)
    iterations_used: int = 0
    suggested_remediation: str | None = None
    narrative: list[str] = field(default_factory=list)
    source_evidence: list[dict] = field(default_factory=list)  # [{source_file, excerpt, relevance}]

    def to_dict(self) -> dict:
        return {
            "root_cause_chain": self.root_cause_chain,
            "confidence": self.confidence,
            "hypothesis": self.hypothesis,
            "narrative": self.narrative,
            "supporting_findings": [f.to_dict() for f in self.supporting_findings],
            "affected_resources": self.affected_resources,
            "iterations_used": self.iterations_used,
            "suggested_remediation": self.suggested_remediation,
            "source_evidence": self.source_evidence,
        }


# ---------------------------------------------------------------------------
# Investigation models (ErrorRootCauseTool internals)
# ---------------------------------------------------------------------------

@dataclass
class TaskRequest:
    task_id: str
    description: str
    target_logs: list[Path] = field(default_factory=list)
    focus_arns: list[str] = field(default_factory=list)
    search_patterns: list[str] = field(default_factory=list)
    context: dict = field(default_factory=dict)


@dataclass
class TaskResult:
    task_id: str
    findings: list[LogFinding] = field(default_factory=list)
    resource_refs: list[str] = field(default_factory=list)
    suggested_next: list[str] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class InvestigationState:
    iteration: int = 0
    max_iterations: int = 10
    findings_so_far: list[LogFinding] = field(default_factory=list)
    investigated_arns: set[str] = field(default_factory=set)
    investigated_logs: set[str] = field(default_factory=set)
    hypothesis: str | None = None
    root_cause_chain: list[str] = field(default_factory=list)
    is_resolved: bool = False
    # Checkpoint metadata
    started_at: datetime = field(default_factory=datetime.utcnow)
    last_checkpoint_at: datetime | None = None
    error_pattern: str = ""
    log_dir: str = ""

    def to_dict(self) -> dict:
        return {
            "iteration": self.iteration,
            "max_iterations": self.max_iterations,
            "findings_so_far": [f.to_dict() for f in self.findings_so_far],
            "investigated_arns": sorted(self.investigated_arns),
            "investigated_logs": sorted(self.investigated_logs),
            "hypothesis": self.hypothesis,
            "root_cause_chain": self.root_cause_chain,
            "is_resolved": self.is_resolved,
            "started_at": self.started_at.isoformat(),
            "last_checkpoint_at": datetime.utcnow().isoformat(),
            "error_pattern": self.error_pattern,
            "log_dir": self.log_dir,
        }

    @classmethod
    def from_dict(cls, data: dict) -> InvestigationState:
        findings = []
        for fd in data.get("findings_so_far", []):
            findings.append(LogFinding(
                source_file=fd["source_file"],
                timestamp=datetime.fromisoformat(fd["timestamp"]) if fd.get("timestamp") else datetime.utcnow(),
                severity=Severity(fd["severity"]),
                message=fd["message"],
                resource_arns=fd.get("resource_arns", []),
                raw_lines=fd.get("raw_lines", []),
                line_numbers=fd.get("line_numbers", []),
                context=fd.get("context", {}),
            ))
        return cls(
            iteration=data.get("iteration", 0),
            max_iterations=data.get("max_iterations", 10),
            findings_so_far=findings,
            investigated_arns=set(data.get("investigated_arns", [])),
            investigated_logs=set(data.get("investigated_logs", [])),
            hypothesis=data.get("hypothesis"),
            root_cause_chain=data.get("root_cause_chain", []),
            is_resolved=data.get("is_resolved", False),
            started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else datetime.utcnow(),
            last_checkpoint_at=datetime.fromisoformat(data["last_checkpoint_at"]) if data.get("last_checkpoint_at") else None,
            error_pattern=data.get("error_pattern", ""),
            log_dir=data.get("log_dir", ""),
        )

    def save(self, path: str) -> None:
        """Save checkpoint to JSON file."""
        import json
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2, default=str))

    @classmethod
    def load(cls, path: str) -> InvestigationState | None:
        """Load checkpoint from JSON file. Returns None if not found."""
        import json
        p = Path(path)
        if not p.exists():
            return None
        try:
            return cls.from_dict(json.loads(p.read_text()))
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Analysis event stream
# ---------------------------------------------------------------------------

class AnalysisEventType(Enum):
    """Event types emitted during analysis for real-time progress tracking."""
    INVESTIGATION_STARTED = "investigation_started"
    LOG_FILE_READING = "log_file_reading"
    LOG_FILE_ANALYZED = "log_file_analyzed"
    FINDINGS_DISCOVERED = "findings_discovered"
    HYPOTHESIS_FORMED = "hypothesis_formed"
    REQUESTING_LOGS = "requesting_logs"
    ITERATION_COMPLETE = "iteration_complete"
    CHECKPOINT_SAVED = "checkpoint_saved"
    CONTEXT_FILE_WRITTEN = "context_file_written"
    INVESTIGATION_COMPLETE = "investigation_complete"
    ERROR = "error"


@dataclass
class AnalysisEvent:
    """A single event emitted during analysis."""
    event_type: AnalysisEventType
    message: str
    data: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)
