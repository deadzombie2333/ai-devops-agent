# SQL Server Log Simulation Environment

Simulates a production SQL Server with workload activity and exports logs to S3. Replicates the TokenAndPermUserStore memory leak pattern observed in the original `test_logs/` data.

## Architecture

```
EventBridge (10 min) → Simulator Lambda → RDS SQL Server Express
EventBridge (6 hrs)  → Log Exporter Lambda → S3 Bucket
Manual invoke        → Reset Lambda → cleans everything
```

- **RDS SQL Server Express** (`db.t3.small`, 2 GB RAM) — the database generating logs
- **S3 Bucket** (`sqlserver-log-export-{account}`) — all logs land here, no CloudWatch
- **3 Lambda Functions** — all use pymssql layer for DB connectivity

## Lambda Functions

### 1. Workload Simulator (`sqlserver-workload-simulator`)
Runs every 10 minutes. 5 phases:

| Phase | What it does |
|-------|-------------|
| Setup | Creates 3 databases (SimDB, AppDB, ReportDB) + tables |
| Normal ops | 15-30 random CRUD operations (orders, audit logs, sessions) |
| Login bloat | Creates 20 new SQL logins per run, grants permissions across all 3 DBs — grows TokenAndPermUserStore without cleanup |
| Cache pressure | Connects as 30 random logins across databases — grows TokenPerm/TokenAudit entries |
| Errors | Failed logins (Error 18456), nonexistent procs, bad queries |

### 2. Log Exporter (`sqlserver-log-exporter`)
Runs every 6 hours. Exports two types of logs to S3:

**DMV query results** (same format as `test_logs/`):
- `dmv_logs/sys.dm_os_memory_cache_entries_{date}.csv` — security token cache entries
- `dmv_logs/sys_dm_os_ring_buffers_{date}.rpt` — ring buffer security cache snapshots
- `dmv_logs/syslogins_{date}.csv` — all server logins
- `dmv_logs/syslogins_sid_recent_{date}.rpt` — recently created logins

**RDS error/agent logs**:
- `error_logs/` — SQL Server error log files downloaded via RDS API

### 3. Reset (`sqlserver-reset`)
Manual invoke only. Cleans everything:
- Drops all `sim_user_*` logins and database users
- Drops SimDB, AppDB, ReportDB
- Clears TokenAndPermUserStore cache (`DBCC FREESYSTEMCACHE`)
- Deletes all S3 log files

## Deployment Steps

### 1. Create the S3 bucket (needed before stack for the Lambda layer)
```bash
aws s3api create-bucket --bucket sqlserver-log-export-183017937161 \
  --region us-east-2 --create-bucket-configuration LocationConstraint=us-east-2
```

### 2. Build and upload pymssql Lambda layer
```bash
mkdir -p /tmp/pymssql-layer/python
pip install pymssql -t /tmp/pymssql-layer/python --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.11
cd /tmp/pymssql-layer && zip -r pymssql-layer.zip python/
aws s3 cp pymssql-layer.zip s3://sqlserver-log-export-183017937161/layers/pymssql-layer.zip
```

### 3. Deploy CloudFormation stack
```bash
aws cloudformation deploy \
  --template-file sqlserver-sim/template.yaml \
  --stack-name sqlserver-sim \
  --parameter-overrides DBMasterPassword='YourSecurePassword123!' \
  --capabilities CAPABILITY_IAM \
  --region us-east-2
```

### 4. Verify
```bash
# Check stack status
aws cloudformation describe-stacks --stack-name sqlserver-sim --region us-east-2

# Manually trigger simulator
aws lambda invoke --function-name sqlserver-workload-simulator /dev/stdout --region us-east-2

# Manually trigger log export
aws lambda invoke --function-name sqlserver-log-exporter /dev/stdout --region us-east-2

# Reset everything
aws lambda invoke --function-name sqlserver-reset /dev/stdout --region us-east-2
```

## What the original logs showed

The `test_logs/` ring buffer data showed `TokenAndPermUserStore pages_kb` growing from **40 MB → 96 GB** over time. `EntriesInserted` consistently exceeded `EntriesRemoved` across all token types (SecContextToken, LoginToken, UserToken, TokenPerm, TokenAudit), indicating the security cache never properly cleaned up — a known SQL Server memory leak that eventually crashes the server.

This simulation replicates that pattern by creating many logins and permission grants without ever calling `DBCC FREESYSTEMCACHE`. On Express edition (1 GB memory cap), it won't reach 96 GB, but will generate memory pressure warnings and the same log structure.

## Cost Estimate

- RDS `db.t3.small`: ~$0.034/hr (~$25/month)
- Lambda: negligible (short runs, low frequency)
- S3: negligible (small log files)
- Secrets Manager: ~$0.40/month

Use the [AWS Pricing Calculator](https://calculator.aws/) for exact estimates.

## Cleanup
```bash
aws cloudformation delete-stack --stack-name sqlserver-sim --region us-east-2
aws s3 rb s3://sqlserver-log-export-183017937161 --force
```
