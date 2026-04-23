"""Log Analyzer Agent — concurrent file analysis via Claude Code CLI.

Each file gets its own CC session running in parallel (up to max_concurrent).
max_turns is calculated dynamically based on file size.
CC reads files directly using its own tools — no Python file I/O.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logger = logging.getLogger(__name__)

# Size thresholds
_MAX_FILE_SIZE = 50_000_000    # 50MB — skip files larger than this


class LogAnalyzerAgent:
    """Analyzes log files concurrently by letting Claude Code CLI read them directly."""

    def __init__(self, model: str = "claude-sonnet-4.5", credit_tracker=None,
                 max_concurrent: int = 3) -> None:
        self.model = model
        self.credit_tracker = credit_tracker
        self.max_concurrent = max_concurrent

    def analyze(self, log_paths: list[Path]) -> str:
        """Analyze log files concurrently. Returns combined text analysis."""
        if not log_paths:
            return ""

        # Filter out oversized files
        valid_paths = []
        for p in log_paths:
            size = p.stat().st_size
            if size > _MAX_FILE_SIZE:
                logger.warning("Skipping %s (%s) — too large", p.name, _human_size(p))
            elif size == 0:
                logger.debug("Skipping empty file: %s", p.name)
            else:
                valid_paths.append(p)

        if not valid_paths:
            return ""

        logger.info("Analyzing %d files with %d concurrent workers",
                     len(valid_paths), min(self.max_concurrent, len(valid_paths)))

        # Run all files concurrently
        results: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=self.max_concurrent) as pool:
            futures = {
                pool.submit(self._analyze_single, p): p
                for p in valid_paths
            }
            for future in as_completed(futures):
                path = futures[future]
                try:
                    text = future.result()
                    if text.strip():
                        results[str(path)] = text.strip()
                        logger.info("✓ %s: %d chars", path.name, len(text))
                    else:
                        logger.warning("✗ %s: empty response", path.name)
                except Exception as e:
                    logger.warning("✗ %s: %s", path.name, e)

        if not results:
            return ""

        # Combine results in original file order
        parts = []
        for p in valid_paths:
            key = str(p)
            if key in results:
                parts.append(f"=== {p.name} ===\n{results[key]}")

        return "\n\n---\n\n".join(parts)

    def _analyze_single(self, path: Path) -> str:
        """Analyze a single file in its own CC session."""
        from aws_devops_ai.infra.ask_claude import ClaudeSession, AuthExpiredError

        abs_path = str(path.resolve())

        prompt = f"""Read and analyze this log file:

FILE: {path.name} ({_human_size(path)})
PATH: {abs_path}

Read the file and report:
- File format and structure
- Key findings: errors, warnings, anomalies, security issues, performance problems
- Resource identifiers: ARNs, hostnames, IPs, service names, database names, server names
- Timestamps of notable events
- For large files, focus on patterns and significant entries — don't list every row

Be thorough but concise."""

        session = ClaudeSession(
            model=self.model,
            credit_tracker=self.credit_tracker,
            label=f"analyzer:{path.name}",
        )
        try:
            response = session.start(prompt)
            return response
        except AuthExpiredError:
            raise
        except Exception as e:
            logger.warning("Failed to analyze %s: %s", path.name, e)
            return ""
        finally:
            session.end()


def _human_size(path: Path) -> str:
    size = path.stat().st_size
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"
