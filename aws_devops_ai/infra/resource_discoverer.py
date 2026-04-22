"""Resource Discoverer — builds and maintains a resource map from AWS APIs and log findings."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from aws_devops_ai.models import (
    LogFinding,
    ResourceEdge,
    ResourceMap,
    ResourceNode,
)

logger = logging.getLogger(__name__)

_ARN_RE = re.compile(r"arn:aws:[a-z0-9\-]+:[a-z0-9\-]*:\d*:[a-zA-Z0-9\-_/:.]+")


def _resource_type_from_arn(arn: str) -> str:
    """Extract resource type from an ARN (e.g. 'lambda:function')."""
    parts = arn.split(":")
    if len(parts) >= 6:
        service = parts[2]
        resource = parts[5].split("/")[0] if "/" in parts[5] else parts[5]
        return f"{service}:{resource}"
    return "unknown"


def _normalize_s3_arn(arn: str) -> str:
    """Normalize S3 ARNs with path components to the bucket-level ARN.

    e.g. arn:aws:s3:::my-bucket/prod/rds/ → arn:aws:s3:::my-bucket
    """
    if arn.startswith("arn:aws:s3:::"):
        bucket_and_path = arn[len("arn:aws:s3:::"):]
        bucket = bucket_and_path.split("/")[0]
        return f"arn:aws:s3:::{bucket}"
    return arn


def _name_from_arn(arn: str) -> str:
    """Extract a short name from an ARN."""
    arn = _normalize_s3_arn(arn)
    parts = arn.split(":")
    if len(parts) >= 6:
        tail = parts[-1]
        if "/" in tail:
            # Filter out empty segments (e.g. trailing slashes)
            segments = [s for s in tail.split("/") if s]
            return segments[-1] if segments else tail
        return tail
    return arn


class ResourceDiscoverer:
    """Builds and maintains a resource map of AWS resources and their connections."""

    def __init__(self, aws_session=None, map_path: str = "./resource_map.json") -> None:
        self.aws_session = aws_session
        self.map_path = Path(map_path)
        self._resource_map = self._load_or_create()

    def _load_or_create(self) -> ResourceMap:
        if self.map_path.exists():
            data = json.loads(self.map_path.read_text())
            return ResourceMap.from_dict(data)
        return ResourceMap()

    # ------------------------------------------------------------------
    # AWS discovery (placeholder — deferred)
    # ------------------------------------------------------------------

    def discover_resources(self) -> ResourceMap:
        """Query AWS APIs to discover resources. Placeholder for now."""
        if self.aws_session is None:
            logger.info("No AWS session — skipping API-based resource discovery")
            return self._resource_map

        # Future: query EC2, Lambda, S3, RDS, API Gateway via boto3
        logger.info("AWS resource discovery not yet implemented")
        return self._resource_map

    # ------------------------------------------------------------------
    # Findings-based enrichment
    # ------------------------------------------------------------------

    def update_from_findings(self, findings: list[LogFinding]) -> ResourceMap:
        """Enrich resource map with ARNs and inferred edges from log findings."""
        existing_edge_keys = {
            (e.source_arn, e.target_arn, e.relationship)
            for e in self._resource_map.edges
        }

        for finding in findings:
            # Normalize S3 path ARNs to bucket-level before adding nodes
            normalized_arns = []
            for arn in finding.resource_arns:
                norm = _normalize_s3_arn(arn)
                if norm not in [a for a in normalized_arns]:
                    normalized_arns.append(norm)

            # Add nodes
            for arn in normalized_arns:
                if arn not in self._resource_map.nodes:
                    self._resource_map.nodes[arn] = ResourceNode(
                        arn=arn,
                        resource_type=_resource_type_from_arn(arn),
                        name=_name_from_arn(arn),
                        region=self._region_from_arn(arn),
                    )

            # Infer edges with correct direction.
            # Determine the log source service from the first ARN in the
            # finding (the resource whose log file we are reading).  When
            # a second ARN appears in the message it is usually the
            # *other* party.  The direction depends on the relationship:
            #   - "blocks" / "access_denied_to" → log source is the actor
            #   - everything else (reads_from, writes_to, depends_on, …)
            #     → the log source is the *caller*, so the caller depends
            #       on / reads from / writes to the other resource.
            # However, the log source is not always the caller.  For
            # infrastructure services (RDS, S3, DynamoDB) the log source
            # is the *target* being accessed, and the mentioned ARN is
            # the caller.  We detect this by checking the service of the
            # first ARN.
            if len(normalized_arns) >= 2:
                log_source_arn = normalized_arns[0]
                other_arns = normalized_arns[1:]
                log_source_svc = _resource_type_from_arn(log_source_arn).split(":")[0]

                # Services whose logs describe *incoming* requests (the
                # mentioned ARN is the caller, not the target).
                _TARGET_SERVICES = {"rds", "s3", "dynamodb", "sqs", "sns", "kinesis"}

                for other_arn in other_arns:
                    relationship = self._infer_relationship(log_source_arn, other_arn, finding.message)

                    if log_source_svc in _TARGET_SERVICES:
                        # Log source is the target; the other ARN is the caller.
                        source, target = other_arn, log_source_arn
                    else:
                        # Log source is the caller (e.g. Lambda, API Gateway).
                        source, target = log_source_arn, other_arn

                    key = (source, target, relationship)
                    if key not in existing_edge_keys:
                        self._resource_map.edges.append(
                            ResourceEdge(source, target, relationship)
                        )
                        existing_edge_keys.add(key)

        self._resource_map.last_updated = datetime.utcnow()
        self.save()
        return self._resource_map

    @staticmethod
    def _infer_relationship(source_arn: str, target_arn: str, message: str) -> str:
        """Infer edge relationship from ARN types and finding message."""
        msg = message.lower()
        if "invok" in msg:
            return "invokes"
        if "read" in msg or "get" in msg or "query" in msg or "select" in msg:
            return "reads_from"
        if "write" in msg or "put" in msg or "insert" in msg or "update" in msg:
            return "writes_to"
        if "timeout" in msg or "latency" in msg or "slow" in msg:
            return "depends_on"
        if "denied" in msg or "unauthorized" in msg or "forbidden" in msg:
            return "access_denied_to"
        if "route" in msg or "backend" in msg or "forward" in msg:
            return "routes_to"
        if "publish" in msg or "notify" in msg:
            return "publishes_to"
        if "block" in msg or "lock" in msg or "contention" in msg:
            return "blocks"
        if "replica" in msg or "replication" in msg:
            return "replicates_to"
        # Default: co-occurrence
        return "connected_to"

    def get_resource_map(self) -> ResourceMap:
        return self._resource_map

    def save(self) -> None:
        self.map_path.parent.mkdir(parents=True, exist_ok=True)
        self.map_path.write_text(json.dumps(self._resource_map.to_dict(), indent=2, default=str))

    @staticmethod
    def _region_from_arn(arn: str) -> str:
        parts = arn.split(":")
        if len(parts) >= 4 and parts[3]:
            return parts[3]
        return "us-east-1"
