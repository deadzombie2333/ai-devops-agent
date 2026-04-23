"""CLI entry point for AWS DevOps AI — invoke tools manually."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from aws_devops_ai.models import LogSource, LogSourceType, SystemConfig, TriggerContext
from aws_devops_ai.tool_registry import ModuleRegistry, ToolRegistry
from aws_devops_ai.tools.error_root_cause_tool import ErrorRootCauseTool
from aws_devops_ai.tools.topology_update_tool import TopologyUpdateTool

logger = logging.getLogger(__name__)


def build_registry(config: SystemConfig) -> tuple[ToolRegistry, ModuleRegistry]:
    """Create ModuleRegistry and ToolRegistry with all built-in tools."""
    modules = ModuleRegistry(config)
    registry = ToolRegistry(modules)
    registry.register(TopologyUpdateTool())
    registry.register(ErrorRootCauseTool())
    return registry, modules


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AWS DevOps AI — tool runner")
    parser.add_argument("tool", help="Tool name to invoke (e.g. topology_update, error_root_cause)")
    parser.add_argument("--log-dir", default="./logs", help="Local log directory")
    parser.add_argument("--tracker-db", default="./tracking.db", help="SQLite tracker DB path")
    parser.add_argument("--topology-path", default="./topology.json")
    parser.add_argument("--resource-map-path", default="./resource_map.json")
    parser.add_argument("--audit-log-path", default="./topology_audit.jsonl")
    parser.add_argument("--source-dir", default=None, help="Local log source directory (e.g. test_logs)")
    parser.add_argument("--target-arn", default=None, help="Target ARN for error investigation")
    parser.add_argument("--error-pattern", default=None, help="Error pattern to investigate")
    parser.add_argument("--resume", action="store_true", help="Resume from last checkpoint (error_root_cause only)")
    parser.add_argument("--params-json", default=None, help="Extra params as JSON string")
    parser.add_argument("--retention-days", type=int, default=7)
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    sources = []
    if args.source_dir:
        sources.append(LogSource(LogSourceType.LOCAL_FILE, args.source_dir))

    config = SystemConfig(
        log_dir=args.log_dir,
        tracker_db=args.tracker_db,
        resource_map_path=args.resource_map_path,
        topology_path=args.topology_path,
        topology_audit_log_path=args.audit_log_path,
        retention_days=args.retention_days,
        sources=sources,
    )

    registry, modules = build_registry(config)

    # Build tool params
    params: dict = {}
    if args.params_json:
        params = json.loads(args.params_json)
    if args.source_dir:
        params.setdefault("sources", config.sources)
    if args.target_arn:
        params["target_arn"] = args.target_arn
    if args.error_pattern:
        params["error_pattern"] = args.error_pattern
    if args.resume:
        params["resume"] = True

    # Attach trigger context
    trigger = TriggerContext(trigger_type="manual")
    params["trigger_context"] = trigger

    print(f"Invoking tool: {args.tool}")
    result = registry.invoke(args.tool, params)

    print(f"\nStatus: {result.status}")
    if result.status == "error":
        print(f"Error: {result.metadata}")
        modules.download_tracker.close()
        return 1

    # Print summary
    if hasattr(result.data, "__dataclass_fields__"):
        for field_name in result.data.__dataclass_fields__:
            val = getattr(result.data, field_name)
            if isinstance(val, (int, float, str)):
                print(f"  {field_name}: {val}")
    else:
        print(f"  data: {result.data}")

    modules.download_tracker.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
