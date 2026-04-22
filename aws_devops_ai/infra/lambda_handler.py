"""Lambda handler — receives S3/CloudWatch events and forwards to EC2 via SSM."""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Environment variables expected:
#   EC2_INSTANCE_ID  — target EC2 instance running the DevOps AI agent
#   TOOL_NAME        — tool to invoke (default: topology_update)
#   LOG_DIR          — remote log directory on EC2 (default: /opt/devops-ai/logs)

EC2_INSTANCE_ID = os.environ.get("EC2_INSTANCE_ID", "")
DEFAULT_TOOL = os.environ.get("TOOL_NAME", "topology_update")
LOG_DIR = os.environ.get("LOG_DIR", "/opt/devops-ai/logs")


def _parse_s3_event(event: dict) -> dict:
    """Extract trigger context from an S3 event notification."""
    records = event.get("Records", [])
    if not records:
        return {"trigger_type": "s3_event", "event_data": event}
    rec = records[0]
    bucket = rec.get("s3", {}).get("bucket", {}).get("name", "")
    key = rec.get("s3", {}).get("object", {}).get("key", "")
    return {
        "trigger_type": "s3_event",
        "event_data": {"bucket": bucket, "key": key, "raw": rec},
    }


def _parse_cloudwatch_event(event: dict) -> dict:
    """Extract trigger context from a CloudWatch event."""
    detail = event.get("detail", {})
    source = event.get("source", "")
    return {
        "trigger_type": "cloudwatch_event",
        "event_data": {"source": source, "detail": detail},
        "target_arn": detail.get("resourceArn"),
        "error_pattern": detail.get("errorPattern"),
    }


def _detect_event_type(event: dict) -> str:
    """Detect whether this is an S3 or CloudWatch event."""
    if "Records" in event and event["Records"]:
        first = event["Records"][0]
        if "s3" in first:
            return "s3"
    if "source" in event and "detail" in event:
        return "cloudwatch"
    return "unknown"


def _send_command(instance_id: str, tool: str, trigger_json: str) -> dict:
    """Forward the tool invocation to EC2 via SSM send_command."""
    import boto3

    ssm = boto3.client("ssm")
    command = (
        f"cd /opt/devops-ai && "
        f"python -m aws_devops_ai.cli {tool} "
        f"--log-dir {LOG_DIR} "
        f"--params-json '{trigger_json}'"
    )
    response = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [command]},
        TimeoutSeconds=600,
    )
    command_id = response["Command"]["CommandId"]
    logger.info("SSM command %s sent to %s", command_id, instance_id)
    return {"command_id": command_id, "instance_id": instance_id}


def handler(event: dict, context) -> dict:
    """Lambda entry point."""
    logger.info("Received event: %s", json.dumps(event)[:500])

    if not EC2_INSTANCE_ID:
        return {"statusCode": 500, "body": "EC2_INSTANCE_ID not configured"}

    event_type = _detect_event_type(event)
    if event_type == "s3":
        trigger = _parse_s3_event(event)
        tool = DEFAULT_TOOL
    elif event_type == "cloudwatch":
        trigger = _parse_cloudwatch_event(event)
        tool = "error_root_cause" if trigger.get("error_pattern") else DEFAULT_TOOL
    else:
        logger.warning("Unknown event type, forwarding as-is")
        trigger = {"trigger_type": "unknown", "event_data": event}
        tool = DEFAULT_TOOL

    trigger_json = json.dumps(trigger)
    result = _send_command(EC2_INSTANCE_ID, tool, trigger_json)

    return {
        "statusCode": 200,
        "body": json.dumps({"tool": tool, "trigger_type": trigger["trigger_type"], **result}),
    }
