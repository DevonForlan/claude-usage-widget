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
when NO official reading has EVER been captured (Claude Code never
configured with the statusLine hook, or the capture file has never
successfully parsed).

REVISED (2026-08-30): a fixed "discard the whole reading after N minutes
without a fresh statusLine render" policy (the old DEFAULT_MAX_AGE) produced
a confusing UI: after ~30 quiet minutes the last known official numbers
vanished and the display fell back to the historical estimate, which - since
local activity was also quiet - showed a misleading "ESTIMATED ~0%" that
looked like a real, current reading of zero usage. The fix: the last
successfully captured five_hour/seven_day reading is now kept indefinitely
(see OfficialRateLimitProvider.fetch(), which no longer rejects a record for
being old), and each window's own `resets_at` - not a fixed clock timeout -
decides whether that reading still describes the CURRENT window. A reading
is still valid to show as-is for as long as `resets_at` hasn't passed yet,
however long ago it was captured; once `resets_at` passes, that number
belongs to a window that is already over, so it is labelled accordingly
rather than either presented as current or replaced by a fake zero.
five_hour and seven_day reset at different times, so this is decided per
window, not for the reading as a whole.

REVISED (2026-09-11): once `resets_at` passes, the last known percentage is
now kept on screen (not blanked out) - the earlier "awaiting refresh" state
used to hide the number entirely, which meant a long-idle 5H window went
blank exactly when a quiet gap made that number most worth keeping visible.
STATUS_LAST_KNOWN_EXPIRED (formerly STATUS_AWAITING_REFRESH) still means
the same thing structurally - `now >= resets_at` - but callers are now
expected to keep showing `used_percentage` alongside it, clearly labelled
as last-known/stale rather than current, until a fresh capture replaces it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

DEFAULT_CAPTURE_PATH = Path.home() / ".claude-usage-widget" / "official_rate_limits.json"

# Below this age, a reading is shown as "LIVE" (statusLine is actively
# rendering); above it - but still before its window's own resets_at - it is
# shown as "LAST KNOWN" (still correct, just not from a moment ago). This is
# purely a display distinction, not a validity cutoff: see module docstring.
LIVE_THRESHOLD = timedelta(minutes=2)

# Per-window status returned by RateLimitWindow.status().
STATUS_LIVE = "live"
STATUS_LAST_KNOWN = "last_known"
STATUS_LAST_KNOWN_EXPIRED = "last_known_expired"

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


# Fixed English abbreviations rather than strftime's locale-dependent %a -
# on a non-English Windows locale, %a would render as e.g. "週日" instead of
# "Sun", which the widget's fixed-width layout isn't built to expect.
_WEEKDAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def format_reset_time(dt: datetime, tz: timezone = TAIPEI, include_date: bool = False) -> str:
    """'13:10'-style label in the given timezone (default Asia/Taipei), from
    an aware UTC datetime. No timestamp, no timezone suffix - the widget's
    audience is one person in one timezone.

    With include_date=True, returns 'Sun 09/14 13:10' instead - the 5-hour
    window resets within the same day often enough that the bare clock time
    is unambiguous, but a 7-day window's reset can land on a day far enough
    out that "13:10" alone doesn't say which day."""
    local = dt.astimezone(tz)
    if include_date:
        return f"{_WEEKDAY_ABBR[local.weekday()]} {local:%m/%d %H:%M}"
    return local.strftime("%H:%M")


def format_age(captured_at: datetime, now: Optional[datetime] = None) -> str:
    """'30m ago' / '2h 13m ago' - elapsed time since captured_at. `now` is
    injectable for deterministic testing; defaults to the real current time."""
    now = now or datetime.now(timezone.utc)
    elapsed = (now - captured_at).total_seconds()
    if elapsed < 60:
        return "just now"
    minutes_total = int(elapsed // 60)
    hours, minutes = divmod(minutes_total, 60)
    if hours > 0:
        return f"{hours}h {minutes}m ago"
    return f"{minutes}m ago"


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

    def reset_label(self, tz: timezone = TAIPEI, include_date: bool = False) -> str:
        return format_reset_time(self.resets_at, tz, include_date=include_date)

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

    def status(self, captured_at: datetime, now: Optional[datetime] = None) -> str:
        """Whether this window's reading (captured at `captured_at`) still
        describes the CURRENT window, per the per-window resets_at rule
        described in this module's docstring - not a fixed age cutoff.

        STATUS_LAST_KNOWN_EXPIRED does not mean "discard used_percentage" -
        it means the window has rolled over and this percentage is now
        stale; callers still show it, just clearly labelled as such (see
        widget.py's _render_window)."""
        now = now or datetime.now(timezone.utc)
        if now >= self.resets_at:
            return STATUS_LAST_KNOWN_EXPIRED
        if now - captured_at <= LIVE_THRESHOLD:
            return STATUS_LIVE
        return STATUS_LAST_KNOWN


@dataclass(frozen=True)
class OfficialRateLimits:
    """The last successfully captured reading of both official rate-limit
    windows. Despite the name, this may not be "fresh" - see module
    docstring - each window's own `status()` decides whether it still
    describes the current window."""

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

    def five_hour_status(self, now: Optional[datetime] = None) -> str:
        return self.five_hour.status(self.captured_at, now)

    def seven_day_status(self, now: Optional[datetime] = None) -> str:
        return self.seven_day.status(self.captured_at, now)


class OfficialRateLimitProvider:
    """Reads the rate_limits object Claude Code's own status line receives,
    captured to a stable local file by statusline_hook.py. This is the
    genuine article - Anthropic's own numbers - not a local estimate.

    Returns the last successfully captured reading regardless of its age -
    there is no age-based rejection here (see module docstring for why).
    Callers decide per-window validity via OfficialRateLimits.five_hour_status()
    / seven_day_status()."""

    name = "official_rate_limits"

    def __init__(self, capture_path: Optional[Path] = None) -> None:
        self._capture_path = capture_path or DEFAULT_CAPTURE_PATH

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
