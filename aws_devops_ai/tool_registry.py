"""Tool Registry, DevOpsTool base class, and ModuleRegistry."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from aws_devops_ai.infra.download_tracker import DownloadTracker
from aws_devops_ai.agents.log_analyzer_agent import LogAnalyzerAgent
from aws_devops_ai.infra.log_manager import LogManager
from aws_devops_ai.infra.ask_claude import CreditTracker
from aws_devops_ai.models import SystemConfig, ToolResult
from aws_devops_ai.infra.resource_discoverer import ResourceDiscoverer
from aws_devops_ai.infra.topology_manager import TopologyManager

logger = logging.getLogger(__name__)


class DevOpsTool(ABC):
    """Abstract base class for all DevOps tools."""

    name: str = ""
    description: str = ""
    parameters: dict = {}

    @abstractmethod
    def execute(self, params: dict, modules: "ModuleRegistry") -> ToolResult:
        ...


class ModuleRegistry:
    """Provides shared, reusable module instances to tools."""

    def __init__(self, config: SystemConfig) -> None:
        self.config = config
        self.credit_tracker = CreditTracker()
        # Only create download tracker/log manager if tracker_db is configured
        if config.tracker_db:
            self.download_tracker = DownloadTracker(config.tracker_db)
            self.log_manager = LogManager(
                log_dir=config.log_dir,
                tracker=self.download_tracker,
                aws_session=None,
                retention_days=config.retention_days,
            )
        else:
            self.download_tracker = None
            self.log_manager = None
        self.resource_discoverer = ResourceDiscoverer(
            aws_session=None,
            map_path=config.resource_map_path,
        )
        self.topology_manager = TopologyManager(
            topology_path=config.topology_path,
            audit_log_path=config.topology_audit_log_path,
        )
        self.log_analyzer_agent = LogAnalyzerAgent(
            model=config.low_resource_model,
            credit_tracker=self.credit_tracker,
            max_concurrent=config.max_concurrent_analyzers,
        )


class ToolRegistry:
    """Registers and exposes modular tools to users and triggers."""

    def __init__(self, modules: ModuleRegistry) -> None:
        self._modules = modules
        self._tools: dict[str, DevOpsTool] = {}

    def register(self, tool: DevOpsTool) -> None:
        self._tools[tool.name] = tool

    def get_tool(self, name: str) -> DevOpsTool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[dict]:
        return [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in self._tools.values()
        ]

    def invoke(self, tool_name: str, params: dict) -> ToolResult:
        tool = self._tools.get(tool_name)
        if tool is None:
            logger.error("Unknown tool: %r. Available: %s", tool_name, list(self._tools.keys()))
            return ToolResult(
                tool_name=tool_name,
                status="error",
                data=None,
                metadata={"error": f"Unknown tool: {tool_name!r}", "error_type": "KeyError"},
            )
        try:
            return tool.execute(params, self._modules)
        except Exception as e:
            logger.error("Tool %s failed: %s", tool_name, e)
            return ToolResult(
                tool_name=tool_name,
                status="error",
                data=None,
                metadata={"error": str(e), "error_type": type(e).__name__},
            )
