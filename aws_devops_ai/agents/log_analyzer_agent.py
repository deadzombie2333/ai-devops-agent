"""Log Analyzer Agent — low-resource AI module for log parsing and extraction."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from aws_devops_ai.models import LogFinding, Severity
from aws_devops_ai.infra.file_readers import read_file_content, is_supported_file

logger = logging.getLogger(__name__)

_ARN_RE = re.compile(r"arn:aws:[a-z0-9\-]+:[a-z0-9\-]*:\d*:[a-zA-Z0-9\-_/:.]+")

_ANALYSIS_PROMPT = """Analyze the following log content and extract structured findings.
Each line is prefixed with its line number (e.g. "L42: ...").

For each notable event (errors, warnings, anomalies), return a JSON array of objects with:
- "severity": one of "info", "warning", "error", "critical"
- "message": concise description of the event
- "resource_arns": list of AWS ARNs mentioned in the event
- "timestamp": ISO timestamp if available, otherwise null
- "line_numbers": list of line numbers (integers) where this event appears (e.g. [42, 43, 44])
- "raw_lines": the exact relevant log lines (copy them verbatim)

Return ONLY a JSON array, no other text.

Log content:
{log_content}"""


class LogAnalyzerAgent:
    """Reads and analyzes log files using AI (KiroSession) to extract findings."""

    def __init__(self, model: str = "claude-sonnet-4.5", credit_tracker=None) -> None:
        self.model = model
        self.credit_tracker = credit_tracker

    def analyze(self, log_paths: list[Path]) -> list[LogFinding]:
        """Analyze log files and return structured findings."""
        from aws_devops_ai.infra.ask_kiro import KiroSession, AuthExpiredError

        all_findings: list[LogFinding] = []
        input_files = {str(p) for p in log_paths}

        for log_path in log_paths:
            if not log_path.exists():
                logger.warning("Log file not found: %s", log_path)
                continue

            content = read_file_content(log_path)
            if not content.strip():
                continue

            # Prefix each line with its line number for traceability
            numbered_lines = []
            for i, line in enumerate(content.splitlines(), 1):
                numbered_lines.append(f"L{i}: {line}")
            numbered_content = "\n".join(numbered_lines)

            session = KiroSession(model=self.model, credit_tracker=self.credit_tracker, label=f"log-analyzer:{log_path.name}")
            try:
                prompt = _ANALYSIS_PROMPT.format(log_content=numbered_content[:50000])  # cap at 50k chars
                response = session.start(prompt)
                findings = self._parse_response(response, str(log_path))

                # Validate source_file is in input set
                for f in findings:
                    if f.source_file in input_files:
                        all_findings.append(f)
                    else:
                        logger.warning("Finding source_file %s not in input set, skipping", f.source_file)

            except AuthExpiredError:
                logger.error("Auth expired during analysis of %s", log_path)
                raise
            except Exception as e:
                logger.warning("Failed to analyze %s: %s", log_path, e)
            finally:
                session.end()

        return all_findings

    def extract_resources(self, log_content: str) -> list[str]:
        """Extract AWS ARNs from log content."""
        return list(set(_ARN_RE.findall(log_content)))

    def _parse_response(self, response: str, source_file: str) -> list[LogFinding]:
        """Parse AI response into LogFinding objects."""
        # Try to extract JSON array from response
        try:
            # Find JSON array in response
            start = response.find("[")
            end = response.rfind("]") + 1
            if start >= 0 and end > start:
                data = json.loads(response[start:end])
            else:
                logger.warning("No JSON array found in response")
                return []
        except json.JSONDecodeError:
            logger.warning("Failed to parse JSON from AI response")
            return []

        findings = []
        for item in data:
            try:
                severity_str = item.get("severity", "info").lower()
                severity = Severity(severity_str) if severity_str in [s.value for s in Severity] else Severity.INFO

                ts_str = item.get("timestamp")
                timestamp = datetime.fromisoformat(ts_str) if ts_str else datetime.utcnow()

                findings.append(LogFinding(
                    source_file=source_file,
                    timestamp=timestamp,
                    severity=severity,
                    message=item.get("message", ""),
                    resource_arns=item.get("resource_arns", []),
                    raw_lines=item.get("raw_lines", []),
                    line_numbers=[int(n) for n in item.get("line_numbers", []) if str(n).isdigit()],
                ))
            except Exception as e:
                logger.warning("Failed to parse finding: %s", e)
                continue

        return findings
