"""Log Manager — download, retention, and re-download of log files."""

from __future__ import annotations

import logging
import shutil
import time
import random
from datetime import datetime, timedelta
from pathlib import Path

from aws_devops_ai.infra.download_tracker import DownloadTracker
from aws_devops_ai.infra.file_readers import SUPPORTED_EXTENSIONS
from aws_devops_ai.models import (
    LogNotFoundError,
    LogReference,
    LogSource,
    LogSourceType,
)

logger = logging.getLogger(__name__)


class LogManager:
    """Downloads logs from AWS or local sources, tracks them, enforces retention."""

    def __init__(
        self,
        log_dir: str,
        tracker: DownloadTracker,
        aws_session=None,  # boto3.Session — None for local-only mode
        retention_days: int = 7,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.tracker = tracker
        self.aws_session = aws_session
        self.retention_days = retention_days

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_new_logs(self, sources: list[LogSource]) -> list[LogReference]:
        """Discover logs not yet downloaded from given sources."""
        downloaded = self.tracker.get_all_downloaded()
        new_refs: list[LogReference] = []

        for source in sources:
            try:
                refs = self._enumerate_source(source)
                for ref in refs:
                    if ref.unique_id not in downloaded:
                        new_refs.append(ref)
            except Exception as e:
                logger.warning("Source %s unreachable: %s", source.identifier, e)
                continue

        return new_refs

    def _enumerate_source(self, source: LogSource) -> list[LogReference]:
        """List available logs from a single source."""
        if source.source_type == LogSourceType.LOCAL_FILE:
            return self._enumerate_local(source)
        # AWS sources — deferred
        if source.source_type == LogSourceType.CLOUDWATCH:
            return self._enumerate_cloudwatch(source)
        if source.source_type == LogSourceType.CLOUDTRAIL:
            return self._enumerate_cloudtrail(source)
        if source.source_type == LogSourceType.S3_BUCKET:
            return self._enumerate_s3(source)
        return []

    def _enumerate_local(self, source: LogSource) -> list[LogReference]:
        """Scan a local directory for supported log/data files (recursively)."""
        src_dir = Path(source.identifier)
        if not src_dir.is_dir():
            raise FileNotFoundError(f"Local source dir not found: {src_dir}")

        if source.prefix:
            # User-specified glob pattern
            patterns = [source.prefix]
        else:
            # Default: all supported extensions
            patterns = [f"*{ext}" for ext in SUPPORTED_EXTENSIONS]

        refs = []
        seen = set()
        for pattern in patterns:
            for f in sorted(src_dir.rglob(pattern)):
                if f.is_file() and f.name not in seen and not f.name.startswith("."):
                    seen.add(f.name)
                    refs.append(LogReference(
                        source=source,
                        key=str(f.relative_to(src_dir)),
                        timestamp=datetime.utcfromtimestamp(f.stat().st_mtime),
                        size_bytes=f.stat().st_size,
                    ))
        return refs

    def _enumerate_cloudwatch(self, source: LogSource) -> list[LogReference]:
        """Enumerate CloudWatch log streams — placeholder for AWS integration."""
        logger.info("CloudWatch enumeration for %s — not yet implemented", source.identifier)
        return []

    def _enumerate_cloudtrail(self, source: LogSource) -> list[LogReference]:
        """Enumerate CloudTrail events — placeholder for AWS integration."""
        logger.info("CloudTrail enumeration for %s — not yet implemented", source.identifier)
        return []

    def _enumerate_s3(self, source: LogSource) -> list[LogReference]:
        """Enumerate S3 objects — placeholder for AWS integration."""
        logger.info("S3 enumeration for %s — not yet implemented", source.identifier)
        return []

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def download_logs(self, log_refs: list[LogReference]) -> list[Path]:
        """Download logs with deduplication tracking. Returns local paths."""
        downloaded_paths: list[Path] = []

        for ref in log_refs:
            if self.tracker.is_downloaded(ref):
                continue  # defensive dedup check

            try:
                local_path = self._download_single(ref)
                self.tracker.mark_downloaded(ref, str(local_path), downloaded_at=datetime.utcnow())
                downloaded_paths.append(local_path)
            except Exception as e:
                logger.warning("Failed to download %s: %s", ref.unique_id, e)
                continue

        return downloaded_paths

    def _download_single(self, ref: LogReference) -> Path:
        """Download a single log to the local log directory."""
        # Build a consistent local filename
        safe_name = ref.unique_id.replace(":", "_").replace("/", "_")
        dest = self.log_dir / safe_name

        if ref.source.source_type == LogSourceType.LOCAL_FILE:
            src = Path(ref.source.identifier) / ref.key
            if not src.exists():
                raise FileNotFoundError(f"Source file not found: {src}")
            shutil.copy2(src, dest)
            return dest

        # AWS sources — with retry
        return self._download_with_retry(ref, dest)

    def _download_with_retry(self, ref: LogReference, dest: Path, max_retries: int = 5) -> Path:
        """Download from AWS with exponential backoff + jitter."""
        for attempt in range(max_retries):
            try:
                return self._download_from_aws(ref, dest)
            except Exception as e:
                if attempt == max_retries - 1:
                    raise
                wait = min(2 ** attempt + random.uniform(0, 1), 30)
                logger.warning("Retry %d/%d for %s: %s (wait %.1fs)", attempt + 1, max_retries, ref.unique_id, e, wait)
                time.sleep(wait)
        raise RuntimeError("Unreachable")  # pragma: no cover

    def _download_from_aws(self, ref: LogReference, dest: Path) -> Path:
        """Download from an AWS source — placeholder."""
        raise NotImplementedError(f"AWS download not yet implemented for {ref.source.source_type.value}")

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def list_local_logs(self, since: datetime | None = None) -> list[Path]:
        """List local log files, optionally filtered by modification time."""
        paths = sorted(self.log_dir.glob("*"))
        if since:
            paths = [p for p in paths if p.is_file() and datetime.utcfromtimestamp(p.stat().st_mtime) >= since]
        else:
            paths = [p for p in paths if p.is_file()]
        return paths

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------

    def enforce_retention(self) -> list[str]:
        """Delete local files older than retention_days, mark purged in tracker."""
        cutoff = datetime.utcnow() - timedelta(days=self.retention_days)
        expired = self.tracker.get_expired_records(before=cutoff)
        purged_ids: list[str] = []

        for record in expired:
            if record.is_purged:
                continue  # already purged

            try:
                if record.local_path:
                    p = Path(record.local_path)
                    if p.exists():
                        p.unlink()
            except OSError as e:
                logger.warning("Failed to delete %s: %s", record.local_path, e)
                continue  # don't mark purged if file delete failed

            self.tracker.mark_purged(record.unique_id)
            purged_ids.append(record.unique_id)

        return purged_ids

    # ------------------------------------------------------------------
    # Re-download
    # ------------------------------------------------------------------

    def redownload_log(self, unique_id: str) -> Path:
        """Re-download a previously purged log using stored metadata."""
        record = self.tracker.get_record(unique_id)
        if record is None:
            raise LogNotFoundError(f"No tracker record for {unique_id}")
        if not record.is_purged:
            raise ValueError(f"Record {unique_id} is not purged — local file should exist at {record.local_path}")

        # Reconstruct source from stored metadata
        source = LogSource(
            source_type=LogSourceType(record.source_type),
            identifier=record.source_identifier,
            region="us-east-1",  # default; local sources don't validate region
        )
        ref = LogReference(source=source, key=record.key, timestamp=datetime.utcnow())

        local_path = self._download_single(ref)
        self.tracker.restore_record(unique_id, str(local_path))
        return local_path
