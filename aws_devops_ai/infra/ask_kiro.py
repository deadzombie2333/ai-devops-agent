import subprocess
import tempfile
import shutil
import re
import sys


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\[\?[0-9;]*[a-zA-Z]")


class AuthExpiredError(Exception):
    """Raised when kiro-cli auth has expired and needs re-login."""
    pass


class CreditTracker:
    """Accumulates credit usage across all KiroSessions in a run."""

    def __init__(self):
        self.sessions: list[dict] = []

    def record(self, label: str, session_stats: dict):
        """Record stats from a completed session."""
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
        lines = ["--- Credit Usage Report ---"]
        lines.append(f"  Total sessions: {len(self.sessions)}")
        lines.append(f"  Total credits:  {self.total_credits:.2f}")
        lines.append(f"  Total time:     {self.total_time_secs}s")
        lines.append(f"  Total turns:    {self.total_turns}")
        lines.append("")
        for i, s in enumerate(self.sessions):
            label = s.get("label", f"session-{i}")
            credits = s.get("total_credits", 0)
            turns = s.get("turns", 0)
            time_s = s.get("total_time_secs", 0)
            model = s.get("model", "unknown")
            lines.append(f"  [{i+1}] {label} ({model}): {credits:.2f} credits, {turns} turns, {time_s}s")
        return "\n".join(lines)


def check_auth():
    """Check if kiro-cli is authenticated. Returns (ok, info_dict)."""
    result = subprocess.run(
        ["kiro-cli", "whoami"],
        capture_output=True, text=True, timeout=15,
    )
    clean = _ANSI_RE.sub("", result.stdout + result.stderr)
    if result.returncode != 0 or "not logged in" in clean.lower() or "error" in clean.lower():
        return False, {"error": clean.strip()}
    info = {"raw": clean.strip()}
    m = re.search(r"Email:\s*(.+)", clean)
    if m:
        info["email"] = m.group(1).strip()
    m = re.search(r"Logged in with (.+)", clean)
    if m:
        info["method"] = m.group(1).strip()
    return True, info


def login(license_type=None, use_device_flow=False, identity_provider=None, region=None):
    """
    Initiate kiro-cli login. Returns (success, message).

    On EC2/remote, set use_device_flow=True. The CLI will print a URL and
    code — a human must open that URL in a browser and enter the code.
    This cannot be fully automated (OAuth requires human approval).
    """
    cmd = ["kiro-cli", "login"]
    if license_type:
        cmd.extend(["--license", license_type])
    if use_device_flow:
        cmd.append("--use-device-flow")
    if identity_provider:
        cmd.extend(["--identity-provider", identity_provider])
    if region:
        cmd.extend(["--region", region])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    clean = _ANSI_RE.sub("", result.stdout + result.stderr).strip()
    return result.returncode == 0, clean

# Credit multipliers per model (from kiro-cli --list-models)
CREDIT_MULTIPLIERS = {
    "auto": 1.0,
    "claude-opus-4.6": 2.2,
    "claude-opus-4.6-1m": 2.2,
    "claude-sonnet-4.6": 1.3,
    "claude-sonnet-4.6-1m": 1.3,
    "claude-opus-4.5": 2.2,
    "claude-sonnet-4.5": 1.3,
    "claude-sonnet-4": 1.3,
    "claude-haiku-4.5": 0.4,
}

# Context window sizes (tokens)
CONTEXT_WINDOWS = {
    "auto": 200_000,
    "claude-opus-4.6": 200_000,
    "claude-opus-4.6-1m": 1_000_000,
    "claude-sonnet-4.6": 200_000,
    "claude-sonnet-4.6-1m": 1_000_000,
    "claude-opus-4.5": 200_000,
    "claude-sonnet-4.5": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-haiku-4.5": 200_000,
}


class KiroSession:
    """
    A persistent kiro-cli chat session.

    Each session uses an isolated temp directory as cwd so multiple
    sessions can run concurrently without colliding on --resume.
    """

    def __init__(self, model=None, trust_all=True, timeout=120, credit_tracker=None, label=None):
        self.model = model
        self.trust_all = trust_all
        self.timeout = timeout
        self._cwd = None
        self._started = False
        self._credit_tracker = credit_tracker
        self._label = label or "unnamed"
        # Usage tracking
        self.turn_count = 0
        self.total_credits = 0.0
        self.total_time_secs = 0
        self.total_input_chars = 0
        self.total_output_chars = 0
        self.last_credits = 0.0
        self.last_time_secs = 0

    def start(self, initial_prompt):
        """Begin a new conversation. Returns the first response."""
        self._cwd = tempfile.mkdtemp(prefix="kiro_session_")
        self._started = True
        return self._run(initial_prompt, resume=False)

    def send(self, prompt):
        """Continue the conversation. Returns the response."""
        if not self._started:
            raise RuntimeError("Session not started. Call start() first.")
        return self._run(prompt, resume=True)

    def end(self):
        """End the session, record credits, and clean up the temp directory."""
        if self._credit_tracker and self.turn_count > 0:
            self._credit_tracker.record(self._label, self.stats)
        if self._cwd:
            shutil.rmtree(self._cwd, ignore_errors=True)
            self._cwd = None
        self._started = False

    @property
    def is_active(self):
        return self._started

    def _build_cmd(self, prompt, resume=False):
        cmd = ["kiro-cli", "chat", "--no-interactive"]
        if self.trust_all:
            cmd.append("-a")
        if self.model:
            cmd.extend(["--model", self.model])
        if resume:
            cmd.append("--resume")
        cmd.append(prompt)
        return cmd

    @staticmethod
    def _strip_ansi(text):
        return _ANSI_RE.sub("", text)

    def _credit_multiplier(self):
        return CREDIT_MULTIPLIERS.get(self.model or "auto", 1.0)

    def _context_window(self):
        return CONTEXT_WINDOWS.get(self.model or "auto", 200_000)

    def _parse_stats(self, clean_output):
        """Extract credits and time from ANSI-stripped output."""
        credits = 0.0
        time_secs = 0
        m = re.search(r"Credits:\s*([\d.]+)", clean_output)
        if m:
            credits = float(m.group(1))
        m = re.search(r"Time:\s*(\d+)s", clean_output)
        if m:
            time_secs = int(m.group(1))
        return credits, time_secs

    def _parse_response(self, raw_stdout, raw_stderr="", input_prompt=""):
        """Extract the assistant response and update usage stats."""
        # Stats are on stderr
        clean_err = self._strip_ansi(raw_stderr)
        credits, time_secs = self._parse_stats(clean_err)
        self.last_credits = credits
        self.last_time_secs = time_secs
        self.total_credits += credits
        self.total_time_secs += time_secs
        self.total_input_chars += len(input_prompt)

        # Response text is on stdout
        clean = self._strip_ansi(raw_stdout)
        lines = clean.strip().splitlines()
        content_lines = []
        for line in lines:
            if line.startswith("> "):
                content_lines.append(line[2:])
            elif content_lines:
                # Multi-line response continuation
                if "▸" in line or "Credits:" in line:
                    break
                content_lines.append(line)
        text = "\n".join(content_lines).strip() if content_lines else clean.strip()
        self.total_output_chars += len(text)
        self.turn_count += 1
        return text

    @property
    def stats(self):
        """Return current session usage stats with context utilization."""
        multiplier = self._credit_multiplier()
        context_window = self._context_window()
        # Estimate base tokens from credits: credits = base_tokens * multiplier * rate
        # Using credits as a proxy: higher credits = more tokens consumed
        est_base_tokens = int(self.total_credits / multiplier * 10_000) if multiplier else 0
        # Also estimate from char counts (rough: ~4 chars per token)
        est_char_tokens = (self.total_input_chars + self.total_output_chars) // 4
        # Use the higher estimate (credits-based is more accurate when available)
        est_tokens = max(est_base_tokens, est_char_tokens)
        utilization_pct = round(est_tokens / context_window * 100, 1) if context_window else 0

        return {
            "model": self.model or "auto",
            "credit_multiplier": multiplier,
            "turns": self.turn_count,
            "total_credits": round(self.total_credits, 2),
            "total_time_secs": self.total_time_secs,
            "est_tokens_used": est_tokens,
            "context_window": context_window,
            "context_utilization_pct": utilization_pct,
        }

    def _run(self, prompt, resume=False):
        cmd = self._build_cmd(prompt, resume=resume)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self.timeout,
            cwd=self._cwd,
        )
        combined = self._strip_ansi(result.stdout + result.stderr).lower()
        if result.returncode != 0:
            if any(kw in combined for kw in ["not logged in", "unauthorized", "expired", "login"]):
                raise AuthExpiredError(
                    "kiro-cli auth expired. Run: kiro-cli login --use-device-flow"
                )
            raise RuntimeError(
                f"kiro-cli exited with code {result.returncode}: {result.stderr}"
            )
        return self._parse_response(result.stdout, result.stderr, input_prompt=prompt)


# --- Convenience function for one-shot queries (no session) ---

def ask_kiro(prompt, model=None, trust_all=True, timeout=120):
    """Send a single stateless prompt to kiro-cli and return the response."""
    session = KiroSession(model=model, trust_all=trust_all, timeout=timeout)
    response = session.start(prompt)
    session.end()
    return response


# --- Demo ---

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
        print("Run: kiro-cli login --use-device-flow")
        sys.exit(1)
    print(f"Authenticated: {info.get('email', 'unknown')}\n")

    # Demo: multi-turn session
    session = KiroSession(model=model)
    try:
        print("Starting session...")
        r1 = session.start("Remember this code: ALPHA-7. Confirm you got it.")
        print(f"Kiro: {r1}")
        print(f"Stats: {session.stats}\n")

        print("Following up...")
        r2 = session.send("What was the code I told you to remember?")
        print(f"Kiro: {r2}")
        print(f"Stats: {session.stats}\n")
    except AuthExpiredError as e:
        print(f"Auth expired mid-session: {e}")
    finally:
        session.end()
        print("Session ended.")
