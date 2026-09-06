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

SEMANTICS (2026-08-31): official_rate_limits.json is a LAST-GOOD SNAPSHOT -
the most recent render that actually carried a usable rate_limits reading,
not simply "whatever the most recent render sent". A render that renders
without a valid reading (rate_limits missing, null, `{}`, or a window
missing/unparsable) must leave the existing file - and its captured_at -
completely untouched. See _capture_if_valid()/_is_valid_window() for what
"usable" means.

Bug fixed here: the previous version wrote whenever `rate_limits` was a
dict at all, with no check that it actually contained anything - so a
render with `rate_limits: {}` (or with only one of five_hour/seven_day
present) silently clobbered the last known good reading with data
OfficialRateLimitProvider can't parse, and the widget's own per-window
resets_at logic never got a chance to run because there was nothing left to
read at all. Reproduced directly: writing a good reading, then a `{}`
reading, left OfficialRateLimitProvider.fetch() returning None even though
the good reading was only moments old.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

CAPTURE_PATH = Path.home() / ".claude-usage-widget" / "official_rate_limits.json"

# Temporary diagnostic log for the last-good-snapshot investigation - one
# JSON line per statusLine render, recording only booleans/reasons about the
# rate_limits shape (never the payload itself, so no prompt/conversation
# content ever lands here). Safe to delete this file or remove the logging
# once the fix is confirmed to hold up over a real quiet period.
LOG_PATH = Path.home() / ".claude-usage-widget" / "statusline_hook.log"


def main() -> int:
    # Some invocation paths (observed: PowerShell piping into a native exe)
    # prepend a UTF-8 BOM even though the payload itself has none - strip it
    # defensively rather than let a stray BOM silently fail json.loads.
    raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
    print(_process(raw))
    return 0


def _process(
    raw: str,
    capture_path: Optional[Path] = None,
    log_path: Optional[Path] = None,
) -> str:
    """The whole hook body, minus stdin/stdout - split out so tests can drive
    it with a payload string and isolated paths instead of real stdin and
    ~/.claude-usage-widget."""
    capture_path = capture_path or CAPTURE_PATH
    log_path = log_path or LOG_PATH

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        # A payload that parses but isn't an object (e.g. `[]` or `"x"`) has
        # no rate_limits to speak of either - treat it the same as an empty
        # payload rather than letting a stray .get() crash the hook.
        payload = {}

    _capture_if_valid(payload.get("rate_limits"), capture_path, log_path)

    return _render_line(payload)


def _capture_if_valid(rate_limits: object, capture_path: Path, log_path: Path) -> None:
    """Writes rate_limits to capture_path ONLY if it is a fully usable
    reading (see _has_valid_rate_limits) - otherwise leaves whatever was
    there before completely alone, since that's still the last known good
    snapshot. Always logs the outcome for diagnostics."""
    has_rate_limits = isinstance(rate_limits, dict)
    five_hour = rate_limits.get("five_hour") if has_rate_limits else None
    seven_day = rate_limits.get("seven_day") if has_rate_limits else None
    five_hour_present = isinstance(five_hour, dict)
    seven_day_present = isinstance(seven_day, dict)
    five_hour_valid = _is_valid_window(five_hour)
    seven_day_valid = _is_valid_window(seven_day)
    should_write = has_rate_limits and five_hour_valid and seven_day_valid

    if not has_rate_limits:
        skip_reason = "no_rate_limits_object"
    elif not five_hour_present and not seven_day_present:
        skip_reason = "" if should_write else "rate_limits_empty"
    elif not five_hour_valid:
        skip_reason = "" if should_write else "five_hour_missing_or_invalid"
    elif not seven_day_valid:
        skip_reason = "" if should_write else "seven_day_missing_or_invalid"
    else:
        skip_reason = ""

    file_existed_before = capture_path.is_file()
    if should_write:
        _write_capture(rate_limits, capture_path)
    file_exists_after = capture_path.is_file()

    _log(log_path, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "has_rate_limits": has_rate_limits,
        "five_hour_present": five_hour_present,
        "seven_day_present": seven_day_present,
        "five_hour_valid": five_hour_valid,
        "seven_day_valid": seven_day_valid,
        "wrote": should_write,
        "skip_reason": skip_reason,
        "file_existed_before": file_existed_before,
        "file_exists_after": file_exists_after,
    })


def _is_valid_window(window: object) -> bool:
    """Mirrors rate_limits.py's _parse_window validation - kept as an
    independent copy rather than an import, since this script must keep
    running with no dependency on the rest of the package (or PySide6) being
    importable."""
    if not isinstance(window, dict):
        return False
    pct = window.get("used_percentage")
    resets = window.get("resets_at")
    if not isinstance(pct, (int, float)) or isinstance(pct, bool):
        return False
    if not isinstance(resets, (int, float)) or isinstance(resets, bool):
        return False
    try:
        datetime.fromtimestamp(resets, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return False
    return True


def _log(log_path: Path, entry: dict) -> None:
    # Logging must never be able to break the hook - a permissions error or
    # full disk here would otherwise take the real statusLine render down
    # with it.
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _write_capture(rate_limits: dict, capture_path: Path) -> None:
    capture_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "rate_limits": rate_limits,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    # Write-then-rename so the widget never reads a half-written file.
    tmp_path = capture_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(record), encoding="utf-8")
    tmp_path.replace(capture_path)


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
