"""Where usage numbers come from.

Everything the UI needs is expressed as a `UsageSnapshot`, produced by a
`UsageProvider`. The UI never talks to a data source directly, so adding
another source later means adding one provider here and changing which one
`build_default_provider()` returns - no UI changes.

RESEARCHED AND CONFIRMED (see README.md for the full trail): Claude Code does
not expose a usage/quota *percentage* to local tools for subscription (OAuth)
accounts. There is no CLI command, no local file with a limit value, and no
API for consumer accounts - only an open, unresolved feature request
(anthropics/claude-code#13585) asking for exactly this. So this file does not
compute a percentage of anything.

What IS real and locally available: per-message token counts inside this
machine's own session transcripts (~/.claude/projects/**/*.jsonl). Summing
those over a recent time window is genuine data - not mock, not a guess - it
is simply a token *count*, not a percentage of a limit whose size is unknown.
`LocalTranscriptUsageProvider` reports that count; `MockUsageProvider` is kept
only for tests/demos and is no longer the default.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Protocol

# Token fields as they appear in transcript message.usage objects. Summed
# together as one "tokens used" number - Claude Code does not document which
# of these count against which rate-limit bucket, so no attempt is made to
# split them into a more precise breakdown than the data itself supports.
_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


@dataclass(frozen=True)
class UsageSnapshot:
    """One reading of usage, in a shape the UI can render directly."""

    total_tokens: int
    """Tokens used within the provider's reporting window."""

    fresh_tokens: int
    """Portion of total_tokens that was newly processed input/output - i.e.
    excluding cache_read_input_tokens, which replays prior context on every
    turn and can dwarf fresh usage without costing (or counting against a
    limit) the same way. 0 for providers that don't distinguish the two."""

    message_count: int
    """Number of assistant messages that contributed to total_tokens."""

    session_count: int
    """Number of distinct sessions that contributed to total_tokens."""

    headline: str
    """Short label for what is being measured, e.g. "Tokens used - last 5h"."""

    detail: str
    """Secondary line, e.g. a breakdown or a caveat. May be empty."""

    badge_text: Optional[str]
    """Short tag shown next to the title (e.g. "MOCK", "LOCAL ESTIMATE").

    None means no badge - reserved for a real, official reading, which no
    provider here produces today.
    """

    taken_at: datetime


class UsageProvider(Protocol):
    """Anything that can produce a UsageSnapshot."""

    name: str

    def fetch(self) -> Optional[UsageSnapshot]:
        """Return a fresh snapshot, or None if unavailable right now."""
        ...


class LocalTranscriptUsageProvider:
    """Sums real token usage from this machine's own Claude Code transcripts.

    Scoped to a trailing time window (default 5 hours, echoing the rolling
    window Claude's own UI calls the "session" limit) rather than "today",
    since the underlying rate limit Anthropic actually enforces is a rolling
    window, not a calendar day.

    This is a genuine local measurement, not an estimate of your plan's quota:
    there is no reliable local source for the limit itself (see module
    docstring), so this number cannot be expressed as a percentage.
    """

    name = "local_transcripts"

    def __init__(
        self,
        window: timedelta = timedelta(hours=5),
        projects_dir: Optional[Path] = None,
    ) -> None:
        self._window = window
        self._projects_dir = projects_dir or (Path.home() / ".claude" / "projects")

    def fetch(self) -> Optional[UsageSnapshot]:
        if not self._projects_dir.is_dir():
            return None

        cutoff = datetime.now(timezone.utc) - self._window

        total_tokens = 0
        fresh_tokens = 0
        message_count = 0
        session_ids: set[str] = set()

        for path in self._candidate_files(cutoff):
            tokens, fresh, messages, touched = self._scan_file(path, cutoff)
            if touched:
                total_tokens += tokens
                fresh_tokens += fresh
                message_count += messages
                session_ids.add(path.stem)

        hours = self._window.total_seconds() / 3600
        window_label = f"{hours:.0f}h" if hours == int(hours) else f"{hours:.1f}h"

        return UsageSnapshot(
            total_tokens=total_tokens,
            fresh_tokens=fresh_tokens,
            message_count=message_count,
            session_count=len(session_ids),
            headline=f"Tokens used - last {window_label}",
            # Kept short deliberately - this renders on a single fixed-width
            # line with no wrapping/eliding, so anything longer just gets cut
            # off invisibly. The fresh/cached breakdown lives in the big
            # number's tooltip instead, where there's room to show it in full.
            detail=f"{message_count} messages · {len(session_ids)} sessions",
            badge_text="LOCAL",
            taken_at=datetime.now(),
        )

    def _candidate_files(self, cutoff: datetime):
        # Cheap filter first: a file's mtime only grows as lines are
        # appended, so one untouched since before the window cannot contain
        # anything inside it - skip opening it at all.
        try:
            for path in self._projects_dir.rglob("*.jsonl"):
                try:
                    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                except OSError:
                    continue
                if mtime >= cutoff:
                    yield path
        except OSError:
            return

    @staticmethod
    def _scan_file(path: Path, cutoff: datetime) -> tuple[int, int, int, bool]:
        """Returns (total_tokens, fresh_tokens, message_count, touched)."""
        tokens = 0
        fresh = 0
        messages = 0
        touched = False
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    ts_raw = entry.get("timestamp")
                    if not ts_raw:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if ts < cutoff:
                        continue

                    usage = (entry.get("message") or {}).get("usage")
                    if not usage:
                        continue

                    tokens += sum(int(usage.get(f) or 0) for f in _TOKEN_FIELDS)
                    # Everything except cache_read counts as "fresh": it's
                    # newly generated or newly written into the cache, not a
                    # replay of context already sent on an earlier turn.
                    fresh += sum(
                        int(usage.get(f) or 0) for f in _TOKEN_FIELDS
                        if f != "cache_read_input_tokens"
                    )
                    messages += 1
                    touched = True
        except OSError:
            return 0, 0, 0, False
        return tokens, fresh, messages, touched


class MockUsageProvider:
    """Fabricated numbers for tests/demos only - not used by the app itself.

    Kept so selftest.py and manual UI checks don't depend on real transcripts
    existing. Earlier versions of this widget used this as the *default*
    provider and rendered it as a percentage; that stopped once the research
    in the module docstring confirmed no real percentage is obtainable, and
    displaying a moving fake percentage risked being mistaken for real data.
    """

    name = "mock"

    def __init__(self, period_seconds: float = 180.0) -> None:
        self._period = period_seconds
        self._origin = time.monotonic()

    def fetch(self) -> UsageSnapshot:
        elapsed = time.monotonic() - self._origin
        phase = (elapsed % self._period) / self._period
        wave = (math.sin(phase * 2 * math.pi - math.pi / 2) + 1) / 2
        tokens = int(5_000 + wave * 250_000)

        return UsageSnapshot(
            total_tokens=tokens,
            fresh_tokens=int(tokens * 0.4),
            message_count=42,
            session_count=3,
            headline="Tokens used - last 5h",
            detail="Sample data - not real",
            badge_text="MOCK",
            taken_at=datetime.now(),
        )


def build_default_provider() -> UsageProvider:
    """The provider the app runs with."""
    return LocalTranscriptUsageProvider()
