"""Claude Code CLI integration — drop-in replacement for ask_kiro.py.

Public interface mirrors ask_kiro exactly:
  - ClaudeSession  (same API as KiroSession: start / send / end)
  - CreditTracker  (unchanged)
  - AuthExpiredError
  - check_auth()
  - ask_claude()   (one-shot convenience, replaces ask_kiro())

Swap imports in the rest of the codebase:
    from aws_devops_ai.infra.ask_claude import ClaudeSession as KiroSession, ...
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import shutil
import re
import sys
import uuid


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\[\?[0-9;]*[a-zA-Z]")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AuthExpiredError(Exception):
    """Raised when Claude Code auth has expired or API key is invalid."""
    pass


# ---------------------------------------------------------------------------
# Credit / Usage Tracker  (identical to ask_kiro)
# ---------------------------------------------------------------------------

class CreditTracker:
    """Accumulates usage across all ClaudeSessions in a run."""

    def __init__(self):
        self.sessions: list[dict] = []

    def record(self, label: str, session_stats: dict):
        entry = {"label": label}
        entry.update(session_stats)
        self.sessions.append(entry)

    @property
    def total_credits(self) -> float:
        return sum(s.get("total_credits", 0) for s in self.sessions)

    @property
    def total_time_secs(self) -> int:
        return sum(s.get("total_time_secs", 0) for s in self.sessions)

    @property
    def total_turns(self) -> int:
        return sum(s.get("turns", 0) for s in self.sessions)

    def summary(self) -> dict:
        return {
            "total_sessions": len(self.sessions),
            "total_credits": round(self.total_credits, 2),
            "total_time_secs": self.total_time_secs,
            "total_turns": self.total_turns,
            "sessions": self.sessions,
        }

    def format_report(self) -> str:
        lines = ["--- Usage Report (Claude Code) ---"]
        lines.append(f"  Total sessions: {len(self.sessions)}")
        lines.append(f"  Total cost:     ${self.total_credits:.4f}")
        lines.append(f"  Total time:     {self.total_time_secs}s")
        lines.append(f"  Total turns:    {self.total_turns}")
        lines.append("")
        for i, s in enumerate(self.sessions):
            label = s.get("label", f"session-{i}")
            cost = s.get("total_credits", 0)
            turns = s.get("turns", 0)
            time_s = s.get("total_time_secs", 0)
            model = s.get("model", "unknown")
            inp = s.get("input_tokens", 0)
            out = s.get("output_tokens", 0)
            lines.append(
                f"  [{i+1}] {label} ({model}): ${cost:.4f}, "
                f"{turns} turns, {time_s}s, {inp}in/{out}out tokens"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Model metadata
# ---------------------------------------------------------------------------

# Claude Code uses Anthropic API pricing (per-token).
# Cost per 1K tokens (input / output) — approximate, update as needed.
MODEL_PRICING = {
    "claude-sonnet-4-6":    {"input": 0.003,  "output": 0.015},
    "claude-sonnet-4-5":    {"input": 0.003,  "output": 0.015},
    "claude-opus-4-6":      {"input": 0.015,  "output": 0.075},
    "claude-opus-4-5":      {"input": 0.015,  "output": 0.075},
    "claude-opus-4-7":      {"input": 0.015,  "output": 0.075},
    "claude-haiku-4-5":     {"input": 0.0008, "output": 0.004},
}

CONTEXT_WINDOWS = {
    "claude-sonnet-4-6":    200_000,
    "claude-sonnet-4-5":    200_000,
    "claude-opus-4-6":      200_000,
    "claude-opus-4-5":      200_000,
    "claude-opus-4-7":      200_000,
    "claude-haiku-4-5":     200_000,
}

# Map kiro model names → claude code model names for easy migration.
# When using a LiteLLM proxy, the model names must match what the proxy
# exposes (check /v1/models). Adjust this table to match your proxy config.
_MODEL_ALIAS = {
    # kiro-style names → proxy model names
    "claude-opus-4.6":      "claude-opus-4-6",
    "claude-opus-4.5":      "claude-opus-4-5",
    "claude-sonnet-4.6":    "claude-sonnet-4-6",
    "claude-sonnet-4.5":    "claude-sonnet-4-5",
    "claude-sonnet-4":      "claude-sonnet-4-5",
    "claude-haiku-4.5":     "claude-haiku-4-5",
    # claude code short aliases → proxy model names
    "opus":                 "claude-opus-4-6",
    "sonnet":               "claude-sonnet-4-6",
    "haiku":                "claude-haiku-4-5",
    "auto":                 "claude-sonnet-4-6",
}

# Default model when none specified
_DEFAULT_MODEL = "claude-sonnet-4-6"


def _resolve_model(model: str | None) -> str:
    """Resolve a kiro-style or alias model name to the proxy model name."""
    if not model:
        return _DEFAULT_MODEL
    return _MODEL_ALIAS.get(model, model)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def check_auth() -> tuple[bool, dict]:
    """Check if Claude Code CLI is available and authenticated.

    Claude Code authenticates via ANTHROPIC_API_KEY env var or
    claude.ai OAuth. Supports ANTHROPIC_BASE_URL for proxy (e.g. LiteLLM).
    """
    # First check the CLI exists
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return False, {"error": "claude CLI not found or not working"}
    except FileNotFoundError:
        return False, {"error": "claude CLI not installed. Run: npm install -g @anthropic-ai/claude-code"}

    version = result.stdout.strip()

    # Check env vars
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")

    # Try a minimal print call to verify auth works
    try:
        test_env = os.environ.copy()
        test_env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"
        test = subprocess.run(
            ["claude", "-p", "--bare", "--max-turns", "1", "Reply with OK"],
            capture_output=True, text=True, timeout=30, env=test_env,
        )
        if test.returncode != 0:
            err = _ANSI_RE.sub("", test.stderr).strip()
            if "api key" in err.lower() or "unauthorized" in err.lower() or "auth" in err.lower():
                return False, {"error": f"Auth failed: {err}"}
            return False, {"error": err}
    except subprocess.TimeoutExpired:
        # Timeout on a simple query likely means auth prompt is blocking
        return False, {"error": "Auth check timed out — may need API key or login"}

    info = {"version": version, "has_api_key": has_key}
    if base_url:
        info["base_url"] = base_url
    return True, info


# ---------------------------------------------------------------------------
# ClaudeSession — drop-in replacement for KiroSession
# ---------------------------------------------------------------------------

class ClaudeSession:
    """
    A persistent Claude Code CLI chat session.

    Uses `claude -p --output-format json` for structured output and
    `--resume <session-id>` / `--continue` for multi-turn conversations.

    Each session gets a unique session-id so multiple sessions can run
    concurrently without collision.
    """

    def __init__(self, model=None, trust_all=True, timeout=120,
                 credit_tracker=None, label=None):
        self.model = _resolve_model(model)
        self.trust_all = trust_all
        self.timeout = timeout
        self._cwd = None
        self._started = False
        self._session_id = str(uuid.uuid4())
        self._credit_tracker = credit_tracker
        self._label = label or "unnamed"
        # Usage tracking
        self.turn_count = 0
        self.total_credits = 0.0  # cost in USD
        self.total_time_secs = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_input_chars = 0
        self.total_output_chars = 0
        self.last_cost = 0.0
        self.last_time_secs = 0

    def start(self, initial_prompt: str) -> str:
        """Begin a new conversation. Returns the first response."""
        self._cwd = tempfile.mkdtemp(prefix="claude_session_")
        self._started = True
        return self._run(initial_prompt, resume=False)

    def send(self, prompt: str) -> str:
        """Continue the conversation. Returns the response."""
        if not self._started:
            raise RuntimeError("Session not started. Call start() first.")
        return self._run(prompt, resume=True)

    def end(self):
        """End the session, record usage, and clean up."""
        if self._credit_tracker and self.turn_count > 0:
            self._credit_tracker.record(self._label, self.stats)
        if self._cwd:
            shutil.rmtree(self._cwd, ignore_errors=True)
            self._cwd = None
        self._started = False

    @property
    def is_active(self):
        return self._started

    # ----- command building -----

    def _build_cmd(self, prompt: str, resume: bool = False) -> list[str]:
        cmd = ["claude", "-p", "--output-format", "json", "--bare"]
        if self.trust_all:
            cmd.append("--dangerously-skip-permissions")
        if self.model:
            cmd.extend(["--model", self.model])
        # Session management: first call uses --session-id to set a known id,
        # subsequent calls use --resume with that id to continue the conversation.
        if resume:
            cmd.extend(["--resume", self._session_id])
        else:
            cmd.extend(["--session-id", self._session_id])
        # Limit turns to 1 per call — we manage multi-turn ourselves
        cmd.extend(["--max-turns", "1"])
        cmd.append(prompt)
        return cmd

    # ----- output parsing -----

    @staticmethod
    def _strip_ansi(text: str) -> str:
        return _ANSI_RE.sub("", text)

    def _estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Estimate USD cost from token counts."""
        pricing = MODEL_PRICING.get(self.model, {"input": 0.003, "output": 0.015})
        return (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1000

    def _context_window(self) -> int:
        return CONTEXT_WINDOWS.get(self.model, 200_000)

    def _parse_json_response(self, raw_stdout: str, input_prompt: str = "") -> str:
        """Parse JSON output from `claude -p --output-format json`."""
        clean = self._strip_ansi(raw_stdout).strip()

        # Claude Code JSON output contains the result message
        try:
            data = json.loads(clean)
        except json.JSONDecodeError:
            # Fallback: treat as plain text
            self.turn_count += 1
            self.total_input_chars += len(input_prompt)
            self.total_output_chars += len(clean)
            return clean

        # Extract response text
        text = data.get("result", "")

        # Extract usage stats from nested structure
        usage = data.get("usage", {})
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cost = data.get("total_cost_usd", 0.0)
        duration_ms = data.get("duration_ms", 0)
        duration_secs = duration_ms // 1000 if duration_ms else 0

        # If cost not provided, estimate from tokens
        if not cost and (input_tokens or output_tokens):
            cost = self._estimate_cost(input_tokens, output_tokens)

        # Update session-id from response if available
        if data.get("session_id"):
            self._session_id = data["session_id"]

        # Accumulate stats
        self.last_cost = cost
        self.last_time_secs = duration_secs
        self.total_credits += cost
        self.total_time_secs += duration_secs
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_input_chars += len(input_prompt)
        self.total_output_chars += len(text)
        self.turn_count += 1

        return text

    @property
    def stats(self) -> dict:
        """Return current session usage stats."""
        context_window = self._context_window()
        est_tokens = self.total_input_tokens + self.total_output_tokens
        # Fallback estimate from chars if no token data
        if not est_tokens:
            est_tokens = (self.total_input_chars + self.total_output_chars) // 4
        utilization_pct = round(est_tokens / context_window * 100, 1) if context_window else 0

        return {
            "model": self.model or _DEFAULT_MODEL,
            "turns": self.turn_count,
            "total_credits": round(self.total_credits, 6),  # USD
            "total_time_secs": self.total_time_secs,
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "est_tokens_used": est_tokens,
            "context_window": context_window,
            "context_utilization_pct": utilization_pct,
        }

    # ----- execution -----

    def _build_env(self) -> dict:
        """Build environment for subprocess, forwarding proxy/auth vars."""
        env = os.environ.copy()
        # Support ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL for LiteLLM proxy etc.
        # These are already in os.environ if set, but we ensure they propagate.
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
            val = os.environ.get(key)
            if val:
                env[key] = val
        # Prevent Claude Code from sending context_management and other
        # experimental beta fields in the request body — LiteLLM /v1/messages
        # passthrough forwards them to Bedrock which rejects the extra fields
        # with "Extra inputs are not permitted".
        env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"
        return env

    def _run(self, prompt: str, resume: bool = False) -> str:
        cmd = self._build_cmd(prompt, resume=resume)
        env = self._build_env()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=self._cwd,
                env=env,
            )
        except subprocess.TimeoutExpired as e:
            # Return partial output if available
            partial = self._strip_ansi(e.stdout or "").strip() if e.stdout else ""
            if partial:
                self.turn_count += 1
                return partial
            raise

        if result.returncode != 0:
            combined = self._strip_ansi(result.stdout + result.stderr).lower()
            if any(kw in combined for kw in ["api key", "unauthorized", "expired", "authentication"]):
                raise AuthExpiredError(
                    "Claude Code auth failed. Set ANTHROPIC_API_KEY or run: claude login"
                )
            raise RuntimeError(
                f"claude exited with code {result.returncode}: "
                f"{self._strip_ansi(result.stderr)}"
            )

        return self._parse_json_response(result.stdout, input_prompt=prompt)


# ---------------------------------------------------------------------------
# Convenience one-shot function
# ---------------------------------------------------------------------------

def ask_claude(prompt: str, model=None, trust_all=True, timeout=120) -> str:
    """Send a single stateless prompt to Claude Code and return the response."""
    session = ClaudeSession(model=model, trust_all=trust_all, timeout=timeout)
    response = session.start(prompt)
    session.end()
    return response


# ---------------------------------------------------------------------------
# Aliases for drop-in compatibility
# ---------------------------------------------------------------------------

# So existing code can do:
#   from aws_devops_ai.infra.ask_claude import KiroSession, ask_kiro, check_auth
KiroSession = ClaudeSession
ask_kiro = ask_claude


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    model = None
    if "--model" in sys.argv:
        idx = sys.argv.index("--model")
        if idx + 1 < len(sys.argv):
            model = sys.argv[idx + 1]

    # Check auth first
    ok, info = check_auth()
    if not ok:
        print(f"Not authenticated: {info}")
        print("Set ANTHROPIC_API_KEY or run: claude login")
        sys.exit(1)
    print(f"Authenticated: {info}\n")

    # Demo: multi-turn session
    session = ClaudeSession(model=model)
    try:
        print("Starting session...")
        r1 = session.start("Remember this code: ALPHA-7. Confirm you got it.")
        print(f"Claude: {r1}")
        print(f"Stats: {session.stats}\n")

        print("Following up...")
        r2 = session.send("What was the code I told you to remember?")
        print(f"Claude: {r2}")
        print(f"Stats: {session.stats}\n")
    except AuthExpiredError as e:
        print(f"Auth failed: {e}")
    finally:
        session.end()
        print("Session ended.")
