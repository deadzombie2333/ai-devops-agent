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

## Quick Start (EC2)

```bash
git clone https://github.com/deadzombie2333/ai-devops-agent
cd ai-devops-agent
bash startup.sh
```

Edit `.env` with your API key, then:

```bash
source .env
.venv/bin/python run_demo.py <log_directory>
```

## Configuration

Set environment variables (or edit `.env`):

```bash
# API connection (DeepSeek example)
export ANTHROPIC_API_KEY="sk-your-deepseek-key"
export ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"
export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1

# Model tiers
export ANTHROPIC_DEFAULT_OPUS_MODEL="deepseek-v4-pro"     # RCA, master agent
export ANTHROPIC_DEFAULT_SONNET_MODEL="deepseek-v4-pro"   # Topology inference
export ANTHROPIC_DEFAULT_HAIKU_MODEL="deepseek-v4-flash"  # Log file reading
```

## Usage

### Interactive Mode

```bash
.venv/bin/python run_demo.py <log_dir> [output_dir]
```

Ask questions like:
- "Build the service topology"
- "Why is the database timing out?"
- "What errors happened in the last hour?"

### Full Analysis Pipeline

```bash
.venv/bin/python run_analysis.py <log_dir_or_s3_path> \
    --model deepseek-v4-pro \
    --mid-model deepseek-v4-pro \
    --low-model deepseek-v4-flash \
    --output-dir ./output
```

This runs topology building followed by root cause analysis automatically.

### Single Query

```bash
.venv/bin/python run_demo.py <log_dir> . "What is causing the connection failures?"
```

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
    ├── ask_kiro.py          # Kiro CLI wrapper (alternative)
    ├── topology_manager.py  # Topology persistence
    ├── log_manager.py       # Log source management
    ├── file_readers.py      # Multi-format file parsing
    └── resource_discoverer.py # AWS resource discovery
```

## Requirements

- Python 3.10+
- Node.js 18+ (for Claude Code CLI)
- Claude Code CLI (`npm install -g @anthropic-ai/claude-code`)
- An Anthropic-compatible API key (Anthropic, DeepSeek, or LiteLLM proxy)

## Network Whitelist (Restricted EC2)

If deploying in a locked-down environment with no open internet, whitelist these outbound destinations:

### Runtime (minimum required)

| Domain | Port | Purpose |
|--------|------|---------|
| `api.deepseek.com` | 443 | All LLM API calls |

### Deployment phase (can be closed after setup)

| Domain | Port | Purpose |
|--------|------|---------|
| `registry.npmjs.org` | 443 | Install Claude Code CLI |
| `pypi.org` | 443 | pip install dependencies |
| `files.pythonhosted.org` | 443 | pip package downloads |
| `rpm.nodesource.com` or `deb.nodesource.com` | 443 | Install Node.js |
| `github.com` | 443 | git clone |

### Optional (S3 log source)

| Domain | Port | Purpose |
|--------|------|---------|
| `s3.<region>.amazonaws.com` | 443 | Download logs from S3 |

### Security Group example

```
Outbound: HTTPS (443) → api.deepseek.com
Inbound:  SSH (22) → your management IP
```

> Tip: If all dependencies are pre-baked into an AMI, the only runtime whitelist entry needed is `api.deepseek.com:443`.
