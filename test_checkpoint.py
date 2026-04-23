"""Quick checkpoint test — verify all modules import and basic operations work."""

import tempfile
import os
from datetime import datetime, timedelta
from pathlib import Path

from aws_devops_ai.models import (
    LogSource, LogSourceType, LogReference, Severity, LogFinding,
    ResourceMap, ResourceNode, ResourceEdge, TopologyMap, TopologyNode,
    TopologyEdge, HealthStatus, ChangeType, ChangeSource,
    TopologyChangeRecord, SystemConfig, ToolResult, DownloadRecord,
)
from aws_devops_ai.infra.download_tracker import DownloadTracker
from aws_devops_ai.infra.log_manager import LogManager


def test_models():
    """Test data model creation and serialization."""
    src = LogSource(LogSourceType.LOCAL_FILE, "test_logs", region="us-east-1")
    ref = LogReference(src, "test.log", datetime.utcnow(), 100)
    assert ref.unique_id == "local_file:test_logs:test.log"

    # ResourceMap round-trip
    rmap = ResourceMap(
        nodes={"arn:aws:lambda:us-east-1:123:function:f1": ResourceNode("arn:aws:lambda:us-east-1:123:function:f1", "lambda:function", "f1")},
        edges=[ResourceEdge("arn:a", "arn:b", "invokes")],
    )
    d = rmap.to_dict()
    rmap2 = ResourceMap.from_dict(d)
    assert len(rmap2.nodes) == 1
    assert len(rmap2.edges) == 1

    # TopologyMap round-trip
    tmap = TopologyMap(
        nodes={"arn:x": TopologyNode("arn:x", "ec2:instance", "web-server")},
        edges=[TopologyEdge("arn:x", "arn:y", "connects_to")],
    )
    d = tmap.to_dict()
    tmap2 = TopologyMap.from_dict(d)
    assert len(tmap2.nodes) == 1
    assert len(tmap2.edges) == 1
    print("  models OK")


def test_download_tracker():
    """Test DownloadTracker CRUD operations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        tracker = DownloadTracker(db_path)

        src = LogSource(LogSourceType.LOCAL_FILE, "test_logs")
        ref = LogReference(src, "a.log", datetime.utcnow())

        # Mark downloaded
        tracker.mark_downloaded(ref, "/tmp/a.log")
        assert tracker.is_downloaded(ref)

        # Idempotent
        tracker.mark_downloaded(ref, "/tmp/a.log")
        assert len(tracker.get_all_downloaded()) == 1

        # Purge
        tracker.mark_purged(ref.unique_id)
        assert tracker.is_purged(ref.unique_id)
        rec = tracker.get_record(ref.unique_id)
        assert rec.local_path is None
        assert rec.is_purged

        # Restore
        tracker.restore_record(ref.unique_id, "/tmp/a_restored.log")
        rec = tracker.get_record(ref.unique_id)
        assert not rec.is_purged
        assert rec.local_path == "/tmp/a_restored.log"

        # Expired records
        old_src = LogSource(LogSourceType.LOCAL_FILE, "test_logs")
        old_ref = LogReference(old_src, "old.log", datetime.utcnow())
        tracker.mark_downloaded(old_ref, "/tmp/old.log", downloaded_at=datetime.utcnow() - timedelta(days=30))
        expired = tracker.get_expired_records(before=datetime.utcnow() - timedelta(days=7))
        assert len(expired) == 1
        assert expired[0].unique_id == old_ref.unique_id

        tracker.close()
        print("  download_tracker OK")


def test_log_manager():
    """Test LogManager with local file source."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_dir = os.path.join(tmpdir, "logs")
        db_path = os.path.join(tmpdir, "test.db")
        tracker = DownloadTracker(db_path)
        mgr = LogManager(log_dir, tracker, retention_days=7)

        # Discover from test_logs/
        src = LogSource(LogSourceType.LOCAL_FILE, "test_logs")
        refs = mgr.discover_new_logs([src])
        assert len(refs) >= 4, f"Expected at least 4 log files, got {len(refs)}"

        # Download
        paths = mgr.download_logs(refs)
        assert len(paths) == len(refs)
        for p in paths:
            assert p.exists()

        # Second discover should find nothing new
        refs2 = mgr.discover_new_logs([src])
        assert len(refs2) == 0

        # List local
        local = mgr.list_local_logs()
        assert len(local) == len(paths)

        tracker.close()
        print("  log_manager OK")


def test_resource_discoverer():
    """Test ResourceDiscoverer with findings-based enrichment."""
    with tempfile.TemporaryDirectory() as tmpdir:
        map_path = os.path.join(tmpdir, "resource_map.json")
        from aws_devops_ai.infra.resource_discoverer import ResourceDiscoverer
        disc = ResourceDiscoverer(aws_session=None, map_path=map_path)

        findings = [
            LogFinding(
                source_file="test.log", timestamp=datetime.utcnow(),
                severity=Severity.ERROR, message="timeout",
                resource_arns=["arn:aws:lambda:us-east-1:123:function:f1", "arn:aws:dynamodb:us-east-1:123:table/orders"],
            ),
        ]
        rmap = disc.update_from_findings(findings)
        assert "arn:aws:lambda:us-east-1:123:function:f1" in rmap.nodes
        assert "arn:aws:dynamodb:us-east-1:123:table/orders" in rmap.nodes
        assert Path(map_path).exists()
        print("  resource_discoverer OK")


def test_topology_manager():
    """Test TopologyManager update, human updates, audit, and export."""
    with tempfile.TemporaryDirectory() as tmpdir:
        topo_path = os.path.join(tmpdir, "topo.json")
        audit_path = os.path.join(tmpdir, "audit.jsonl")
        from aws_devops_ai.infra.topology_manager import TopologyManager
        from aws_devops_ai.models import HumanTopologyUpdate, ChangeSource

        mgr = TopologyManager(topo_path, audit_path)

        # Build a resource map and findings
        rmap = ResourceMap(
            nodes={
                "arn:aws:lambda:us-east-1:123:function:f1": ResourceNode("arn:aws:lambda:us-east-1:123:function:f1", "lambda:function", "f1"),
                "arn:aws:dynamodb:us-east-1:123:table/orders": ResourceNode("arn:aws:dynamodb:us-east-1:123:table/orders", "dynamodb:table", "orders"),
            },
            edges=[ResourceEdge("arn:aws:lambda:us-east-1:123:function:f1", "arn:aws:dynamodb:us-east-1:123:table/orders", "reads_from")],
        )
        findings = [
            LogFinding(
                source_file="test.log", timestamp=datetime.utcnow(),
                severity=Severity.ERROR, message="DynamoDB timeout",
                resource_arns=["arn:aws:dynamodb:us-east-1:123:table/orders"],
            ),
            LogFinding(
                source_file="test.log", timestamp=datetime.utcnow(),
                severity=Severity.WARNING, message="slow response",
                resource_arns=["arn:aws:lambda:us-east-1:123:function:f1"],
            ),
        ]

        topo = mgr.update(findings, rmap)
        assert len(topo.nodes) == 2
        assert len(topo.edges) == 1
        assert topo.nodes["arn:aws:dynamodb:us-east-1:123:table/orders"].health.status == "error"
        assert topo.nodes["arn:aws:lambda:us-east-1:123:function:f1"].health.status == "warning"

        # Human update
        updates = [
            HumanTopologyUpdate(action="add_node", resource_arn="arn:aws:sqs:us-east-1:123:queue", data={"resource_type": "sqs:queue", "name": "order-queue"}),
            HumanTopologyUpdate(action="update_health", resource_arn="arn:aws:lambda:us-east-1:123:function:f1", data={"status": "healthy"}),
            HumanTopologyUpdate(action="annotate", resource_arn="arn:aws:sqs:us-east-1:123:queue", data={"text": "manually added"}),
        ]
        topo = mgr.apply_human_update("jane@example.com", updates)
        assert "arn:aws:sqs:us-east-1:123:queue" in topo.nodes
        assert topo.nodes["arn:aws:lambda:us-east-1:123:function:f1"].health.status == "healthy"

        # Audit trail
        history = mgr.get_change_history()
        assert len(history) > 0
        human_changes = mgr.get_change_history(source=ChangeSource.HUMAN_INPUT)
        assert all(r.source == ChangeSource.HUMAN_INPUT for r in human_changes)

        # DOT export
        dot = mgr.export_dot()
        assert "digraph topology" in dot
        assert "f1" in dot

        # GraphML export
        graphml = mgr.export_graphml()
        assert "<graphml" in graphml
        assert "orders" in graphml

        # Export determinism
        assert mgr.export_dot() == dot
        assert mgr.export_graphml() == graphml

        print("  topology_manager OK")


def test_tool_registry():
    """Test ToolRegistry register, list, invoke."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from aws_devops_ai.tool_registry import ToolRegistry, ModuleRegistry, DevOpsTool
        from aws_devops_ai.models import SystemConfig, ToolResult

        config = SystemConfig(
            log_dir=os.path.join(tmpdir, "logs"),
            tracker_db=os.path.join(tmpdir, "test.db"),
            resource_map_path=os.path.join(tmpdir, "rmap.json"),
            topology_path=os.path.join(tmpdir, "topo.json"),
            topology_audit_log_path=os.path.join(tmpdir, "audit.jsonl"),
        )
        modules = ModuleRegistry(config)
        registry = ToolRegistry(modules)

        # Create a dummy tool
        class DummyTool(DevOpsTool):
            name = "dummy"
            description = "A test tool"
            parameters = {"msg": "str"}
            def execute(self, params, modules):
                return ToolResult(tool_name=self.name, status="success", data=params.get("msg"))

        registry.register(DummyTool())
        assert registry.get_tool("dummy") is not None
        assert len(registry.list_tools()) == 1

        result = registry.invoke("dummy", {"msg": "hello"})
        assert result.status == "success"
        assert result.data == "hello"

        # Unknown tool
        result = registry.invoke("nonexistent", {})
        assert result.status == "error"

        modules.download_tracker.close()
        print("  tool_registry OK")


def test_topology_update_tool():
    """Test TopologyUpdateTool with local file sources (no AI — just download + empty analysis)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from aws_devops_ai.tool_registry import ModuleRegistry
        from aws_devops_ai.tools.topology_update_tool import TopologyUpdateTool
        from aws_devops_ai.models import SystemConfig, LogSource, LogSourceType

        config = SystemConfig(
            log_dir=os.path.join(tmpdir, "logs"),
            tracker_db=os.path.join(tmpdir, "test.db"),
            resource_map_path=os.path.join(tmpdir, "rmap.json"),
            topology_path=os.path.join(tmpdir, "topo.json"),
            topology_audit_log_path=os.path.join(tmpdir, "audit.jsonl"),
            sources=[LogSource(LogSourceType.LOCAL_FILE, "test_logs")],
        )
        modules = ModuleRegistry(config)

        # Monkey-patch analyzer to avoid real AI calls
        modules.log_analyzer_agent.analyze = lambda paths: ""

        tool = TopologyUpdateTool()
        result = tool.execute({"sources": config.sources}, modules)
        assert result.status == "success"
        assert result.data.new_logs_downloaded >= 4

        modules.download_tracker.close()
        print("  topology_update_tool OK")


def test_cli_build_registry():
    """Test CLI build_registry wires everything together."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from aws_devops_ai.cli import build_registry
        from aws_devops_ai.models import SystemConfig

        config = SystemConfig(
            log_dir=os.path.join(tmpdir, "logs"),
            tracker_db=os.path.join(tmpdir, "test.db"),
            resource_map_path=os.path.join(tmpdir, "rmap.json"),
            topology_path=os.path.join(tmpdir, "topo.json"),
            topology_audit_log_path=os.path.join(tmpdir, "audit.jsonl"),
        )
        registry, modules = build_registry(config)

        tools = registry.list_tools()
        tool_names = [t["name"] for t in tools]
        assert "topology_update" in tool_names
        assert "error_root_cause" in tool_names
        assert len(tools) == 2

        modules.download_tracker.close()
        print("  cli_build_registry OK")


def test_package_exports():
    """Test that __init__.py exports all public classes."""
    import aws_devops_ai

    # Spot-check key exports
    assert hasattr(aws_devops_ai, "LogSourceType")
    assert hasattr(aws_devops_ai, "SystemConfig")
    assert hasattr(aws_devops_ai, "ToolRegistry")
    assert hasattr(aws_devops_ai, "TopologyUpdateTool")
    assert hasattr(aws_devops_ai, "ErrorRootCauseTool")
    assert hasattr(aws_devops_ai, "build_registry")
    assert hasattr(aws_devops_ai, "LogManager")
    assert hasattr(aws_devops_ai, "DownloadTracker")
    assert hasattr(aws_devops_ai, "TopologyManager")
    assert hasattr(aws_devops_ai, "ResourceDiscoverer")
    print("  package_exports OK")


def test_lambda_handler_parse():
    """Test Lambda handler event parsing (no actual SSM call)."""
    from aws_devops_ai.infra.lambda_handler import _parse_s3_event, _parse_cloudwatch_event, _detect_event_type

    # S3 event
    s3_event = {"Records": [{"s3": {"bucket": {"name": "my-bucket"}, "object": {"key": "logs/test.log"}}}]}
    assert _detect_event_type(s3_event) == "s3"
    ctx = _parse_s3_event(s3_event)
    assert ctx["trigger_type"] == "s3_event"
    assert ctx["event_data"]["bucket"] == "my-bucket"
    assert ctx["event_data"]["key"] == "logs/test.log"

    # CloudWatch event
    cw_event = {"source": "aws.ec2", "detail": {"resourceArn": "arn:aws:ec2:us-east-1:123:instance/i-abc", "errorPattern": "OOM"}}
    assert _detect_event_type(cw_event) == "cloudwatch"
    ctx = _parse_cloudwatch_event(cw_event)
    assert ctx["trigger_type"] == "cloudwatch_event"
    assert ctx["target_arn"] == "arn:aws:ec2:us-east-1:123:instance/i-abc"
    assert ctx["error_pattern"] == "OOM"

    print("  lambda_handler_parse OK")


def test_investigation_checkpoint():
    """Test InvestigationState save/load round-trip."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from aws_devops_ai.models import InvestigationState, LogFinding, Severity

        state = InvestigationState(
            iteration=3,
            max_iterations=6,
            findings_so_far=[
                LogFinding(
                    source_file="test.log", timestamp=datetime.utcnow(),
                    severity=Severity.ERROR, message="timeout on DB",
                    resource_arns=["arn:aws:rds:us-east-1:123:db:prod"],
                    line_numbers=[42, 43],
                ),
            ],
            investigated_arns={"arn:aws:rds:us-east-1:123:db:prod"},
            investigated_logs={"test.log", "access.csv"},
            hypothesis="DB connection pool exhausted",
            root_cause_chain=["arn:aws:rds:us-east-1:123:db:prod"],
            error_pattern="timeout",
            log_dir="/tmp/logs",
        )

        ckpt_path = os.path.join(tmpdir, "checkpoint.json")
        state.save(ckpt_path)
        assert os.path.exists(ckpt_path)

        loaded = InvestigationState.load(ckpt_path)
        assert loaded is not None
        assert loaded.iteration == 3
        assert loaded.hypothesis == "DB connection pool exhausted"
        assert len(loaded.findings_so_far) == 1
        assert loaded.findings_so_far[0].message == "timeout on DB"
        assert "test.log" in loaded.investigated_logs
        assert "access.csv" in loaded.investigated_logs
        assert loaded.error_pattern == "timeout"

        # Non-existent path returns None
        assert InvestigationState.load(os.path.join(tmpdir, "nope.json")) is None

        print("  investigation_checkpoint OK")


def test_analysis_events():
    """Test AnalysisEvent and AnalysisEventType."""
    from aws_devops_ai.models import AnalysisEvent, AnalysisEventType

    evt = AnalysisEvent(
        event_type=AnalysisEventType.FINDINGS_DISCOVERED,
        message="Found 3 errors",
        data={"count": 3},
    )
    assert evt.event_type == AnalysisEventType.FINDINGS_DISCOVERED
    assert evt.message == "Found 3 errors"
    assert evt.data["count"] == 3
    assert evt.timestamp is not None
    print("  analysis_events OK")


def test_investigation_context_writer():
    """Test InvestigationContextWriter writes and appends correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        from aws_devops_ai.infra.investigation_context import InvestigationContextWriter
        from aws_devops_ai.models import TopologyMap, TopologyNode

        topo = TopologyMap(
            nodes={"arn:x": TopologyNode("arn:x", "lambda:function", "my-func")},
            edges=[],
        )

        writer = InvestigationContextWriter(tmpdir)
        path = writer.write_initial(
            error_pattern="OOM killed",
            available_logs=["app.log", "db.csv"],
            topology=topo,
        )
        assert os.path.exists(path)
        content = open(path).read()
        assert "OOM killed" in content
        assert "app.log" in content
        assert "my-func" in content

        # Append analysis text
        writer.append_analysis("Found memory leak in connection pool", ["app.log"])
        content2 = open(path).read()
        assert "memory leak" in content2
        assert len(content2) > len(content)

        print("  investigation_context_writer OK")


if __name__ == "__main__":
    print("Checkpoint tests:")
    test_models()
    test_download_tracker()
    test_log_manager()
    test_resource_discoverer()
    test_topology_manager()
    test_tool_registry()
    test_topology_update_tool()
    test_cli_build_registry()
    test_package_exports()
    test_lambda_handler_parse()
    test_investigation_checkpoint()
    test_analysis_events()
    test_investigation_context_writer()
    print("All checkpoint tests passed.")
