"""Official Anthropic rate-limit data, as fed to Claude Code's own status
line and captured locally by statusline_hook.py.

RESEARCHED AND CONFIRMED (2026-08-17): contrary to this project's earlier
conclusion (see usage_source.py's original docstring and README's "Why not a
percentage" section), the JSON Claude Code feeds its own status line DOES
include a real, official `rate_limits` object with five_hour/seven_day
`used_percentage` and `resets_at` fields. Verified directly: a dedicated
statusLine capture script dumped the raw payload across multiple renders in
a real interactive session, and the same two fields came back consistently
(e.g. five_hour 57.99999999999999%, seven_day 89%) - not inferred, not
mocked. This supersedes the prior "no percentage available" finding for
these two specific windows.

It remains true that no source exposes *fresh_tokens* as a percentage
against Anthropic's actual limit - which is why LocalTranscriptUsageProvider
(usage_source.py) and the historical exhaustion anchor (exhaustion.py) both
stay in place: the former as a genuine local token count shown alongside
(not instead of) the official numbers, the latter as a fallback estimate for
when the official reading is unavailable (Claude Code closed, status line
hasn't rendered recently, capture file missing/stale).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

DEFAULT_CAPTURE_PATH = Path.home() / ".claude-usage-widget" / "official_rate_limits.json"

# The statusLine hook only fires when Claude Code re-renders it (a prompt
# sent, a tool call, etc.) - not on a timer of its own. Data older than this
# is treated as unavailable rather than shown as if it were still current.
DEFAULT_MAX_AGE = timedelta(minutes=30)

# A fixed UTC+8 offset rather than zoneinfo.ZoneInfo("Asia/Taipei"): Windows
# Python has no bundled IANA tzdata (ZoneInfo raises ZoneInfoNotFoundError
# without the separate `tzdata` pip package), and Taiwan has kept a constant
# UTC+8 offset with no DST since 1946, so a fixed offset is exactly correct
# here while avoiding an extra dependency entirely.
TAIPEI = timezone(timedelta(hours=8))

# (threshold, level) pairs, checked from the top down.
_WARNING_THRESHOLDS = (
    (95, "critical"),
    (85, "high"),
    (70, "elevated"),
)


def warning_level(percentage: float) -> str:
    """Maps a used-percentage to a named pressure level.

    < 70: normal, 70-84: elevated, 85-94: high, >= 95: critical.
    """
    for threshold, level in _WARNING_THRESHOLDS:
        if percentage >= threshold:
            return level
    return "normal"


def format_reset_time(dt: datetime, tz: timezone = TAIPEI) -> str:
    """'Reset 13:10'-style label in the given timezone (default Asia/Taipei),
    from an aware UTC datetime. No timestamp, no timezone suffix - the
    widget's audience is one person in one timezone."""
    return dt.astimezone(tz).strftime("%H:%M")


@dataclass(frozen=True)
class RateLimitWindow:
    """One rate-limit bucket (either the 5-hour or the 7-day window)."""

    used_percentage: float
    resets_at: datetime  # aware, UTC

    @property
    def rounded_percentage(self) -> int:
        """Reasonable rounding for display - 57.99999999999999 -> 58, not a
        truncation to 57. Float noise like this is expected: it's exactly
        what Anthropic's own API has been observed to send."""
        return round(self.used_percentage)

    @property
    def level(self) -> str:
        return warning_level(self.used_percentage)

    def reset_label(self, tz: timezone = TAIPEI) -> str:
        return format_reset_time(self.resets_at, tz)

    def time_until_reset(self, now: Optional[datetime] = None) -> str:
        """'2h 13m' / '45m' style countdown to resets_at. `now` is injectable
        for deterministic testing; defaults to the real current time."""
        now = now or datetime.now(timezone.utc)
        remaining = (self.resets_at - now).total_seconds()
        if remaining <= 0:
            return "due now"
        minutes_total = int(remaining // 60)
        hours, minutes = divmod(minutes_total, 60)
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"


@dataclass(frozen=True)
class OfficialRateLimits:
    """A complete, fresh reading of both official rate-limit windows."""

    five_hour: RateLimitWindow
    seven_day: RateLimitWindow
    captured_at: datetime  # aware, UTC

    @property
    def overall_percentage(self) -> float:
        """Overall pressure is driven by whichever window is closer to
        exhaustion - the two are independent limits Anthropic enforces
        separately, so they are never averaged or summed."""
        return max(self.five_hour.used_percentage, self.seven_day.used_percentage)

    @property
    def overall_level(self) -> str:
        return warning_level(self.overall_percentage)


class OfficialRateLimitProvider:
    """Reads the rate_limits object Claude Code's own status line receives,
    captured to a stable local file by statusline_hook.py. This is the
    genuine article - Anthropic's own numbers - not a local estimate."""

    name = "official_rate_limits"

    def __init__(
        self,
        capture_path: Optional[Path] = None,
        max_age: timedelta = DEFAULT_MAX_AGE,
    ) -> None:
        self._capture_path = capture_path or DEFAULT_CAPTURE_PATH
        self._max_age = max_age

    def fetch(self) -> Optional[OfficialRateLimits]:
        try:
            raw = self._capture_path.read_text(encoding="utf-8-sig")
        except OSError:
            return None
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            return None

        captured_at = _parse_iso(record.get("captured_at"))
        if captured_at is None:
            return None
        if datetime.now(timezone.utc) - captured_at > self._max_age:
            return None

        rate_limits = record.get("rate_limits")
        if not isinstance(rate_limits, dict):
            return None

        five_hour = _parse_window(rate_limits.get("five_hour"))
        seven_day = _parse_window(rate_limits.get("seven_day"))
        if five_hour is None or seven_day is None:
            return None

        return OfficialRateLimits(five_hour=five_hour, seven_day=seven_day, captured_at=captured_at)


def _parse_window(raw: object) -> Optional[RateLimitWindow]:
    if not isinstance(raw, dict):
        return None
    pct = raw.get("used_percentage")
    resets = raw.get("resets_at")
    if not isinstance(pct, (int, float)) or isinstance(pct, bool):
        return None
    if not isinstance(resets, (int, float)) or isinstance(resets, bool):
        return None
    return RateLimitWindow(
        used_percentage=float(pct),
        resets_at=datetime.fromtimestamp(resets, tz=timezone.utc),
    )


def _parse_iso(raw: object) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
