#!/bin/bash
# EC2 Quick Setup for ai-devops-agent (DeepSeek backend)
set -e

echo "=== AI DevOps Agent - EC2 Setup ==="

# Install Node.js (required for Claude Code CLI)
if ! command -v node &> /dev/null; then
    echo "Installing Node.js..."
    curl -fsSL https://rpm.nodesource.com/setup_20.x | sudo bash - 2>/dev/null || \
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - 2>/dev/null
    sudo yum install -y nodejs 2>/dev/null || sudo apt-get install -y nodejs 2>/dev/null
fi

# Install Claude Code CLI
if ! command -v claude &> /dev/null; then
    echo "Installing Claude Code CLI..."
    npm install -g @anthropic-ai/claude-code
fi

# Python venv setup
if [ ! -d ".venv" ]; then
    echo "Creating Python venv..."
    python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt

# Configure environment
ENV_FILE=".env"
if [ ! -f "$ENV_FILE" ]; then
    echo "Creating .env file — edit with your DeepSeek API key"
    cat > "$ENV_FILE" << 'EOF'
# DeepSeek API Configuration
export ANTHROPIC_API_KEY="sk-YOUR_DEEPSEEK_API_KEY"
export ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"
export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1

# Model Configuration (flash for log reading, pro for analysis)
export ANTHROPIC_DEFAULT_HAIKU_MODEL="deepseek-v4-flash"
export ANTHROPIC_DEFAULT_SONNET_MODEL="deepseek-v4-pro"
export ANTHROPIC_DEFAULT_OPUS_MODEL="deepseek-v4-pro"
EOF
    echo "⚠️  Edit .env and set your ANTHROPIC_API_KEY, then run: source .env"
else
    echo "Loading .env..."
    source "$ENV_FILE"
fi

echo ""
echo "=== Setup Complete ==="
echo "Usage:"
echo "  source .env"
echo "  .venv/bin/python run_demo.py <log_dir>              # Interactive mode"
echo "  .venv/bin/python run_analysis.py <log_dir>          # Full analysis"
