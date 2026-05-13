# AI DevOps Agent

An AI-powered multi-agent system for automated DevOps incident investigation. It reads log files, builds service topology maps, and traces root causes of errors — replacing manual log analysis with AI-driven workflows.

## Architecture

The system uses a **three-tier model hierarchy** with specialized agents:

```
┌─────────────────────────────────────────────────┐
│  MasterAgent (high-resource model)              │
│  - User-facing orchestrator                     │
│  - Routes tasks to tools                        │
│  - Summarizes results                           │
└────────────┬────────────────────┬───────────────┘
             │                    │
┌────────────▼──────┐  ┌─────────▼───────────────┐
│ TopologyUpdateTool │  │ ErrorRootCauseTool      │
│ (mid-resource)     │  │ (high-resource)         │
│ - Infers service   │  │ - Iterative RCA         │
│   connections      │  │ - Hypothesis testing    │
│ - Builds topology  │  │ - Evidence gathering    │
└────────────────────┘  └─────────────────────────┘
             │                    │
┌────────────▼────────────────────▼───────────────┐
│  LogAnalyzerAgent (low-resource model)          │
│  - Concurrent file reading (3 workers)          │
│  - Extracts findings from logs                  │
│  - File-level caching                           │
│  - Supports: .log .csv .rpt .xel .pdf .docx    │
│    .txt .json .xml                              │
└─────────────────────────────────────────────────┘
```

**LLM Backend**: Uses Claude Code CLI (`claude -p`) as the execution engine. Supports any Anthropic-compatible API (DeepSeek, LiteLLM proxy, etc.) via `ANTHROPIC_BASE_URL`.

---

## Part 1: Customer EC2 Setup (Infrastructure)

The customer needs to provision an EC2 instance with the following configuration.

### Step 1: Create Security Group

Create a security group with minimal network access:

**Inbound Rules:**

| Type | Port | Source | Purpose |
|------|------|--------|---------|
| SSH | 22 | EC2 Instance Connect IP range* | Console connect access |

**Outbound Rules:**

| Type | Port | Destination | Purpose |
|------|------|-------------|---------|
| HTTPS | 443 | 0.0.0.0/0 | DeepSeek API + S3 + package install |

> *EC2 Instance Connect IP ranges vary by region. Look up your region at:
> `curl -s https://ip-ranges.amazonaws.com/ip-ranges.json | jq '.prefixes[] | select(.service=="EC2_INSTANCE_CONNECT" and .region=="YOUR_REGION")'`

### Step 2: Create IAM Role

Create an IAM role for the EC2 instance:

- **Trusted entity**: EC2 (`ec2.amazonaws.com`)
- **Policy**: `AmazonS3ReadOnlyAccess` (if logs are stored in S3)
- Create an Instance Profile and attach the role

### Step 3: Launch EC2 Instance

| Parameter | Value |
|-----------|-------|
| AMI | Amazon Linux 2023 (latest x86_64) |
| Instance Type | `t3.small` (minimum: 2 vCPU, 2GB RAM) |
| Storage | 20GB gp3 |
| Key Pair | Customer's existing key pair |
| Security Group | The one created in Step 1 |
| IAM Instance Profile | The one created in Step 2 |
| Public IP | Enabled (required for EC2 Instance Connect via console) |
| Subnet | Public subnet with internet gateway |

### Step 4: Verify Connectivity

Connect via AWS Console → EC2 → Connect (EC2 Instance Connect), then verify:

```bash
# Should succeed (returns "Authentication Fails" = network is open, just no API key)
curl -m 5 https://api.deepseek.com

# Should fail (only HTTPS 443 is allowed outbound)
curl -m 5 http://www.google.com
```

---

## Part 2: Service Deployment

Once the EC2 is ready, deploy the AI DevOps Agent.

### Step 1: Install System Dependencies

```bash
sudo dnf install -y git nodejs npm python3-pip
```

### Step 2: Clone and Setup

```bash
git clone https://github.com/deadzombie2333/ai-devops-agent
cd ai-devops-agent
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install requests boto3 pydantic pymupdf pytesseract Pillow
sudo npm install -g @anthropic-ai/claude-code
```

### Step 3: Configure Environment

```bash
cat > .env << 'EOF'
export ANTHROPIC_API_KEY="sk-your-deepseek-api-key"
export ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"
export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1

# Model tiers: flash for log reading, pro for analysis
export ANTHROPIC_DEFAULT_HAIKU_MODEL="deepseek-v4-flash"
export ANTHROPIC_DEFAULT_SONNET_MODEL="deepseek-v4-pro"
export ANTHROPIC_DEFAULT_OPUS_MODEL="deepseek-v4-pro"
EOF

source .env
```

### Step 4: Verify Claude Code CLI Works

```bash
claude -p --bare --max-turns 1 "Say hi"
```

Expected: a greeting response from DeepSeek.

### Step 5: Prepare Log Files

Option A — Download from S3:
```bash
aws s3 cp s3://your-bucket/your-logs/ ./test_logs/ --recursive
```

Option B — Copy logs directly to the instance:
```bash
mkdir test_logs
# scp or place log files into test_logs/
```

### Step 6: Run Analysis

```bash
# Single query mode
.venv/bin/python run_demo.py test_logs . "What errors are happening and what is the root cause?"

# Interactive mode
.venv/bin/python run_demo.py test_logs

# Full pipeline (topology + RCA)
.venv/bin/python run_analysis.py test_logs --output-dir ./output
```

---

## Usage Examples

### Interactive Mode

```bash
.venv/bin/python run_demo.py <log_dir> [output_dir]
```

Ask questions like:
- "Build the service topology"
- "Why is the database timing out?"
- "What errors happened in the last hour?"

### Single Query

```bash
.venv/bin/python run_demo.py <log_dir> . "What is causing the connection failures?"
```

### Full Analysis Pipeline

```bash
.venv/bin/python run_analysis.py <log_dir_or_s3_path> \
    --model deepseek-v4-pro \
    --mid-model deepseek-v4-pro \
    --low-model deepseek-v4-flash \
    --output-dir ./output
```

---

## Cost Reference

Based on testing with 2 log files (~50KB total):

| Agent | Model | Cost (RMB) | Time |
|-------|-------|------------|------|
| Log Analyzer (file 1) | deepseek-v4-flash | ¥0.04 | 25s |
| Log Analyzer (file 2) | deepseek-v4-flash | ¥0.04 | 42s |
| RCA Master | deepseek-v4-pro | ¥0.07 | 181s |
| **Total** | | **¥0.15** | **~4 min** |

---

## Project Structure

```
aws_devops_ai/
├── master_agent.py          # User-facing orchestrator
├── models.py                # Data models and config
├── cli.py                   # Registry builder
├── tool_registry.py         # Tool registration and module wiring
├── agents/
│   └── log_analyzer_agent.py   # Concurrent log file reader
├── tools/
│   ├── topology_update_tool.py # Service topology builder
│   └── error_root_cause_tool.py # Root cause analysis
└── infra/
    ├── ask_claude.py        # Claude Code CLI wrapper
    ├── topology_manager.py  # Topology persistence
    ├── log_manager.py       # Log source management
    ├── file_readers.py      # Multi-format file parsing
    └── resource_discoverer.py # AWS resource discovery
```

## Network Whitelist (Locked-Down Environment)

### Runtime (minimum required)

| Domain | Port | Purpose |
|--------|------|---------|
| `api.deepseek.com` | 443 | All LLM API calls |

### Deployment phase (can be closed after setup)

| Domain | Port | Purpose |
|--------|------|---------|
| `registry.npmjs.org` | 443 | Install Claude Code CLI |
| `pypi.org` / `files.pythonhosted.org` | 443 | pip install dependencies |
| `github.com` | 443 | git clone |

### Optional (S3 log source)

| Domain | Port | Purpose |
|--------|------|---------|
| `s3.<region>.amazonaws.com` | 443 | Download logs from S3 |

> **Tip**: If all dependencies are pre-baked into an AMI, the only runtime whitelist entry needed is `api.deepseek.com:443`.

## Requirements

- Amazon Linux 2023 (or any Linux with Python 3.9+)
- Node.js 18+
- Claude Code CLI (`sudo npm install -g @anthropic-ai/claude-code`)
- DeepSeek API key (or any Anthropic-compatible API)
- EC2: t3.small minimum, 20GB storage
