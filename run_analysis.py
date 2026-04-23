#!/usr/bin/env python3
"""One-command analysis runner.

Usage:
    # Full run (S3 or local):
    python run_analysis.py --api-url https://proxy.com --api-key sk-... \
        --logs s3://bucket/prefix/ --error "OOM"

    # Skip topology (already built), just run RCA:
    python run_analysis.py --api-url https://proxy.com --api-key sk-... \
        --logs ./test_logs --error "OOM" --skip-topology

    # Resume RCA from checkpoint:
    python run_analysis.py --api-url https://proxy.com --api-key sk-... \
        --logs ./test_logs --error "OOM" --skip-topology --resume

    # Only run topology:
    python run_analysis.py --api-url https://proxy.com --api-key sk-... \
        --logs ./test_logs --only-topology
"""

import argparse
import logging
import os
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AWS DevOps AI — one-command log analysis",
    )

    parser.add_argument("--api-url", default=None, help="LiteLLM proxy URL")
    parser.add_argument("--api-key", default=None, help="API key for the proxy")
    parser.add_argument("--logs", required=True, help="s3://bucket/prefix/ or local dir")
    parser.add_argument("--error", default="", help="Error pattern to investigate")
    parser.add_argument("--resume", action="store_true", help="Resume RCA from checkpoint")
    parser.add_argument("--skip-topology", action="store_true",
                        help="Skip topology step (use existing topology_output/)")
    parser.add_argument("--only-topology", action="store_true",
                        help="Only build topology, skip RCA")
    parser.add_argument("--output-dir", default=".", help="Base output directory")
    parser.add_argument("--model", default=None, help="Opus model for RCA planning")
    parser.add_argument("--mid-model", default=None, help="Sonnet model for topology")
    parser.add_argument("--low-model", default=None, help="Haiku model for file reading")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # --- Configure LLM proxy ---
    if args.api_url:
        base = args.api_url.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        os.environ["ANTHROPIC_BASE_URL"] = base
        logger.info("API URL: %s", base)

    if args.api_key:
        os.environ["ANTHROPIC_API_KEY"] = args.api_key
        logger.info("API key: %s...%s", args.api_key[:5], args.api_key[-4:])

    os.environ["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"

    from aws_devops_ai.models import LogSource, LogSourceType, SystemConfig
    from aws_devops_ai.cli import build_registry

    topo_dir = os.path.join(args.output_dir, "topology_output")
    rca_dir = os.path.join(args.output_dir, "rca_output")

    # --- Resolve log source ---
    if args.logs.startswith("s3://"):
        local_log_dir = os.path.join(args.output_dir, "downloaded_logs")
        logger.info("Downloading logs from %s ...", args.logs)
        downloaded = _download_s3_logs(args.logs, local_log_dir)
        if downloaded == 0:
            logger.error("No supported log files found at %s", args.logs)
            return 1
        logger.info("Downloaded %d log files to %s", downloaded, local_log_dir)
    else:
        local_log_dir = args.logs
        if not os.path.isdir(local_log_dir):
            logger.error("Local log directory not found: %s", local_log_dir)
            return 1
        logger.info("Using local logs: %s", local_log_dir)

    # --- Build config ---
    config = SystemConfig(
        topology_path=os.path.join(topo_dir, "topology.json"),
        topology_audit_log_path=os.path.join(topo_dir, "topology_audit.jsonl"),
        topology_output_dir=topo_dir,
        rca_output_dir=rca_dir,
        resource_map_path=os.path.join(topo_dir, "resource_map.json"),
        tracker_db="",
        log_dir=local_log_dir,
        sources=[LogSource(LogSourceType.LOCAL_FILE, local_log_dir)],
    )
    if args.model:
        config.high_resource_model = args.model
    if args.mid_model:
        config.mid_resource_model = args.mid_model
    if args.low_model:
        config.low_resource_model = args.low_model

    registry, modules = build_registry(config)
    start_time = time.time()

    # --- Step 1: Topology ---
    topo_json = os.path.join(topo_dir, "topology.json")
    if args.skip_topology and os.path.exists(topo_json):
        print("\n  Skipping topology (using existing)")
    else:
        print("\n" + "=" * 60)
        print("  STEP 1: Building service topology from logs")
        print("=" * 60)

        topo_result = registry.invoke("topology_update", {"log_dir": local_log_dir})
        if topo_result.status == "error":
            logger.error("Topology build failed: %s", topo_result.metadata)
            return 1

        topo_data = topo_result.data
        print(f"\n  Topology: {len(topo_data.topology.nodes)} nodes, "
              f"{len(topo_data.topology.edges)} edges from {topo_data.new_logs_downloaded} files")

    if args.only_topology:
        elapsed = time.time() - start_time
        print(f"\n  Done ({elapsed:.0f}s). Output: {topo_dir}/")
        return 0

    # --- Step 2: RCA ---
    print("\n" + "=" * 60)
    print("  STEP 2: Running root cause analysis")
    print("=" * 60 + "\n")

    rca_params = {
        "log_dir": local_log_dir,
        "error_pattern": args.error,
        "resume": args.resume,
    }
    rca_result = registry.invoke("error_root_cause", rca_params)
    if rca_result.status == "error":
        logger.error("RCA failed: %s", rca_result.metadata)
        return 1

    report = rca_result.data
    elapsed = time.time() - start_time

    # --- Summary ---
    print("\n" + "=" * 60)
    print("  ANALYSIS COMPLETE")
    print("=" * 60)
    print(f"\n  Confidence: {report.confidence:.0%}")
    print(f"  Iterations: {report.iterations_used}")
    print(f"  Total time: {elapsed:.0f}s")

    if report.hypothesis:
        print(f"\n  Hypothesis: {report.hypothesis}")

    if report.root_cause_chain:
        print(f"\n  Root cause chain:")
        for i, item in enumerate(report.root_cause_chain):
            prefix = "  ROOT →" if i == 0 else "       →"
            print(f"    {prefix} {item}")

    if report.suggested_remediation:
        print(f"\n  Remediation: {str(report.suggested_remediation)[:200]}")

    output_files = rca_result.metadata.get("output_files", {})
    print(f"\n  Output files:")
    print(f"    Topology:  {topo_dir}/")
    print(f"    RCA JSON:  {output_files.get('json', rca_dir + '/rca_report.json')}")
    print(f"    RCA Report:{output_files.get('md', rca_dir + '/incident_report.md')}")

    credit = modules.credit_tracker
    print(f"\n  {credit.format_report()}")

    return 0


def _download_s3_logs(s3_uri: str, local_dir: str) -> int:
    import boto3
    from pathlib import Path
    from aws_devops_ai.infra.file_readers import SUPPORTED_EXTENSIONS

    os.makedirs(local_dir, exist_ok=True)
    uri = s3_uri.removeprefix("s3://")
    if "/" in uri:
        bucket, prefix = uri.split("/", 1)
    else:
        bucket, prefix = uri, ""
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    count = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            ext = Path(key).suffix.lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue
            if Path(key).name.startswith("."):
                continue
            relative = key[len(prefix):] if prefix else key
            local_path = Path(local_dir) / relative
            local_path.parent.mkdir(parents=True, exist_ok=True)
            logger.info("  Downloading s3://%s/%s", bucket, key)
            s3.download_file(bucket, key, str(local_path))
            count += 1
    return count


if __name__ == "__main__":
    sys.exit(main())
