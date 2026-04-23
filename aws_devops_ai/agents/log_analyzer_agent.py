"""Log Analyzer Agent — concurrent file analysis via Claude Code CLI.

Each file gets its own CC session running in parallel (up to max_concurrent).
CC reads files directly using its own tools — no Python file I/O.

Includes a file-level cache: if the same file (path + size + mtime) was
analyzed before, the cached result is returned without calling CC.
Cache is stored as JSON in .analysis_cache/ next to the log files.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_FILE_SIZE = 50_000_000  # 50MB


class AnalysisCache:
    """File-level cache for CC analysis results.

    Cache key = hash(absolute_path + file_size + mtime).
    Stored as individual JSON files in a cache directory.
    """

    def __init__(self, cache_dir: str = ".analysis_cache") -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _cache_key(path: Path) -> str:
        stat = path.stat()
        raw = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def get(self, path: Path) -> str | None:
        key = self._cache_key(path)
        cache_file = self._dir / f"{key}.json"
        if not cache_file.exists():
            return None
        try:
            data = json.loads(cache_file.read_text())
            # Validate it's for the same file
            if data.get("abs_path") == str(path.resolve()):
                logger.info("Cache hit: %s", path.name)
                return data["result"]
        except (json.JSONDecodeError, KeyError):
            pass
        return None

    def put(self, path: Path, result: str) -> None:
        key = self._cache_key(path)
        cache_file = self._dir / f"{key}.json"
        data = {
            "abs_path": str(path.resolve()),
            "file_name": path.name,
            "file_size": path.stat().st_size,
            "result": result,
        }
        cache_file.write_text(json.dumps(data, ensure_ascii=False))


class LogAnalyzerAgent:
    """Analyzes log files concurrently by letting Claude Code CLI read them directly."""

    def __init__(self, model: str = "claude-sonnet-4.5", credit_tracker=None,
                 max_concurrent: int = 3, cache_dir: str = ".analysis_cache") -> None:
        self.model = model
        self.credit_tracker = credit_tracker
        self.max_concurrent = max_concurrent
        self._cache = AnalysisCache(cache_dir)

    def analyze(self, log_paths: list[Path]) -> str:
        """Analyze log files concurrently. Returns combined text analysis."""
        if not log_paths:
            return ""

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

        # Split into cached vs uncached
        cached_results: dict[str, str] = {}
        uncached_paths: list[Path] = []
        for p in valid_paths:
            hit = self._cache.get(p)
            if hit is not None:
                cached_results[str(p)] = hit
            else:
                uncached_paths.append(p)

        if cached_results:
            logger.info("Cache: %d hits, %d misses", len(cached_results), len(uncached_paths))

        # Analyze uncached files concurrently
        if uncached_paths:
            logger.info("Analyzing %d files with %d concurrent workers",
                         len(uncached_paths), min(self.max_concurrent, len(uncached_paths)))

            with ThreadPoolExecutor(max_workers=self.max_concurrent) as pool:
                futures = {
                    pool.submit(self._analyze_single, p): p
                    for p in uncached_paths
                }
                for future in as_completed(futures):
                    path = futures[future]
                    try:
                        text = future.result()
                        if text.strip():
                            cached_results[str(path)] = text.strip()
                            self._cache.put(path, text.strip())
                            logger.info("✓ %s: %d chars", path.name, len(text))
                        else:
                            logger.warning("✗ %s: empty response", path.name)
                    except Exception as e:
                        logger.warning("✗ %s: %s", path.name, e)

        if not cached_results:
            return ""

        # Combine in original file order
        parts = []
        for p in valid_paths:
            key = str(p)
            if key in cached_results:
                parts.append(f"=== {p.name} ===\n{cached_results[key]}")

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
