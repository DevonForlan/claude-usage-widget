"""Claude Code statusLine hook.

Claude Code invokes whatever `statusLine.command` names in settings.json on
every status line render, feeding it a JSON payload on stdin and using
whatever it prints on stdout as the visible status line text. This script:

1. Captures the `rate_limits` object from that payload (when present) to a
   stable file this widget's OfficialRateLimitProvider reads - see
   rate_limits.py for the full research trail confirming that field is real.
2. Prints back a short, sane status line so Claude Code's own UI shows
   something reasonable instead of debug output.

Wire it in (merges with whatever else is already in settings.json - see
install.ps1):

    "statusLine": {
      "type": "command",
      "command": "<python.exe> <path to this file>"
    }
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

CAPTURE_PATH = Path.home() / ".claude-usage-widget" / "official_rate_limits.json"


def main() -> int:
    # Some invocation paths (observed: PowerShell piping into a native exe)
    # prepend a UTF-8 BOM even though the payload itself has none - strip it
    # defensively rather than let a stray BOM silently fail json.loads.
    raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {}

    rate_limits = payload.get("rate_limits")
    if isinstance(rate_limits, dict):
        _write_capture(rate_limits)

    print(_render_line(payload))
    return 0


def _write_capture(rate_limits: dict) -> None:
    CAPTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "rate_limits": rate_limits,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    # Write-then-rename so the widget never reads a half-written file.
    tmp_path = CAPTURE_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(record), encoding="utf-8")
    tmp_path.replace(CAPTURE_PATH)


def _render_line(payload: dict) -> str:
    model = (payload.get("model") or {}).get("display_name") or "Claude"
    effort = (payload.get("effort") or {}).get("level")
    ctx = payload.get("context_window") or {}
    used_pct = ctx.get("used_percentage")

    parts = [model]
    if effort:
        parts.append(str(effort))
    if isinstance(used_pct, (int, float)):
        parts.append(f"ctx {used_pct:.0f}%")
    return " · ".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
