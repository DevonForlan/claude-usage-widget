"""Estimated Quota Model: turns real local rate-limit hits into an anchor.

This is deliberately separate from usage_source.py's UsageSnapshot, which is
just a token count with no claim about what fraction of a limit it
represents. Everything in this file exists to answer a different, weaker
question: "the last time this account actually got a 429, how much had it
used in the preceding window?" - and to compare today's usage against that.

RESEARCHED (see README.md): Claude Code writes a distinctive, unambiguous
shape into a session transcript when the API returns 429:

    "error": "rate_limit", "isApiErrorMessage": true, "apiErrorStatus": 429

as real JSON keys on the message entry (not as text a human or another tool
merely mentions). Matching only on these exact typed key/value pairs - never
a substring search over the raw line - is what keeps this from mistaking a
conversation *about* rate limits (including this project's own research
chats) for an actual hit. Subagent transcripts are additionally excluded
from detection (not from token counting - see _window_stats) as belt-and-
suspenders, because a subagent doing this exact kind of research can end up
with old error text pasted into its own tool output, carrying a fresh
timestamp but stale content.

This account-wide limit can be hit by several concurrent Claude Code
processes within seconds of each other, each logging their own copy of the
same 429 into their own transcript. Those are clustered into one logical
ExhaustionEvent, not counted as multiple samples.

THE ANCHOR IS NOT A CONFIRMED LIMIT. It is the smallest fresh-token count
this machine has ever observed immediately preceding a real 429 - a
conservative floor, not Anthropic's actual ceiling. `ConservativeMinAnchorStrategy`
is intentionally swappable (see EstimatedQuotaModel) so that once enough
independent events accumulate, a median/percentile-based strategy can
replace it without any change to the widget or the rest of this module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Protocol

DEFAULT_WINDOW = timedelta(hours=5)

# Two concurrent processes hitting the same account-wide limit have been
# observed to log it within ~26 seconds of each other; 120s gives headroom
# without risking merging two genuinely separate real events.
_CLUSTER_TOLERANCE = timedelta(seconds=120)

# Once an event is stored, a rescan finding "the same" timestamp again
# (e.g. a file touched again later still containing the old error line)
# should not be recorded a second time.
_DEDUP_TOLERANCE = timedelta(minutes=5)

_DEFAULT_STORE_PATH = Path.home() / ".claude-usage-widget" / "exhaustion_events.json"


@dataclass(frozen=True)
class ExhaustionEvent:
    """One real, locally-observed 429, with the token usage that preceded it."""

    timestamp: datetime
    window_hours: float
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    message_count: int
    session_count: int
    model_distribution: dict = field(default_factory=dict)
    source_files: list = field(default_factory=list)

    @property
    def fresh_tokens(self) -> int:
        """input + output + cache_creation - excludes replayed context, same
        definition used for UsageSnapshot.fresh_tokens in usage_source.py."""
        return self.input_tokens + self.output_tokens + self.cache_creation_tokens

    @property
    def raw_total_tokens(self) -> int:
        return self.fresh_tokens + self.cache_read_tokens


def _event_to_dict(event: ExhaustionEvent) -> dict:
    return {
        "timestamp": event.timestamp.isoformat(),
        "window_hours": event.window_hours,
        "input_tokens": event.input_tokens,
        "output_tokens": event.output_tokens,
        "cache_creation_tokens": event.cache_creation_tokens,
        "cache_read_tokens": event.cache_read_tokens,
        "message_count": event.message_count,
        "session_count": event.session_count,
        "model_distribution": event.model_distribution,
        "source_files": event.source_files,
    }


def _event_from_dict(raw: dict) -> ExhaustionEvent:
    return ExhaustionEvent(
        timestamp=datetime.fromisoformat(raw["timestamp"]),
        window_hours=float(raw["window_hours"]),
        input_tokens=int(raw["input_tokens"]),
        output_tokens=int(raw["output_tokens"]),
        cache_creation_tokens=int(raw["cache_creation_tokens"]),
        cache_read_tokens=int(raw["cache_read_tokens"]),
        message_count=int(raw["message_count"]),
        session_count=int(raw["session_count"]),
        model_distribution=dict(raw.get("model_distribution") or {}),
        source_files=list(raw.get("source_files") or []),
    )


class ExhaustionStore:
    """Local persistence for discovered exhaustion events.

    Deliberately not under ~/.claude - that directory belongs to Claude Code
    itself. This is the widget's own derived data, kept separately so it is
    never mistaken for something Anthropic wrote, and never touched by
    Claude Code's own housekeeping.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _DEFAULT_STORE_PATH
        self._events: list[ExhaustionEvent] = []
        self._last_scan_cutoff: Optional[datetime] = None
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for raw in data.get("events", []):
            try:
                self._events.append(_event_from_dict(raw))
            except (KeyError, ValueError):
                continue
        cutoff_raw = data.get("last_scan_cutoff")
        if cutoff_raw:
            try:
                self._last_scan_cutoff = datetime.fromisoformat(cutoff_raw)
            except ValueError:
                pass

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "last_scan_cutoff": (
                self._last_scan_cutoff.isoformat() if self._last_scan_cutoff else None
            ),
            "events": [_event_to_dict(e) for e in self._events],
        }
        self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def events(self) -> list[ExhaustionEvent]:
        return list(self._events)

    def last_scan_cutoff(self) -> Optional[datetime]:
        return self._last_scan_cutoff

    def has_nearby_event(self, timestamp: datetime) -> bool:
        return any(
            abs((timestamp - e.timestamp).total_seconds()) <= _DEDUP_TOLERANCE.total_seconds()
            for e in self._events
        )

    def add_event(self, event: ExhaustionEvent) -> bool:
        """Adds the event unless a nearby one is already stored. Returns
        whether it was actually added."""
        if self.has_nearby_event(event.timestamp):
            return False
        self._events.append(event)
        self._events.sort(key=lambda e: e.timestamp)
        return True

    def set_last_scan_cutoff(self, timestamp: datetime) -> None:
        if self._last_scan_cutoff is None or timestamp > self._last_scan_cutoff:
            self._last_scan_cutoff = timestamp


def _is_subagent_transcript(path: Path) -> bool:
    return "subagents" in path.parts


def _is_rate_limit_event(entry: dict) -> bool:
    """True only if the exact typed fields Claude Code writes for a 429 are
    present as real JSON keys - never a text/substring match. A tool result
    that merely contains the string '"isApiErrorMessage":true' inside a
    quoted text value (e.g. someone's own grep output) will not match this,
    since that string isn't parsed into dict keys at any of these positions.
    """
    candidates = [entry, entry.get("message") or {}]
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list):
        candidates.extend(c for c in content if isinstance(c, dict))
    for candidate in candidates:
        if (
            candidate.get("isApiErrorMessage") is True
            and candidate.get("apiErrorStatus") == 429
            and candidate.get("error") == "rate_limit"
        ):
            return True
    return False


def _parse_timestamp(raw: object) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class _RawHit:
    timestamp: datetime
    source_file: Path


def _scan_for_hits(projects_dir: Path, since: Optional[datetime]) -> list[_RawHit]:
    hits: list[_RawHit] = []
    try:
        candidates = list(projects_dir.rglob("*.jsonl"))
    except OSError:
        return hits

    for path in candidates:
        if _is_subagent_transcript(path):
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if since is not None and mtime < since:
            continue
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
                    if not _is_rate_limit_event(entry):
                        continue
                    ts = _parse_timestamp(entry.get("timestamp"))
                    if ts is None:
                        continue
                    if since is not None and ts < since:
                        continue
                    hits.append(_RawHit(timestamp=ts, source_file=path))
        except OSError:
            continue
    return hits


def _cluster_hits(hits: list[_RawHit]) -> list[tuple[datetime, list[Path]]]:
    """Groups hits that landed within _CLUSTER_TOLERANCE of each other - the
    signature of one account-wide 429 logged by several concurrent sessions -
    into a single (canonical_timestamp, source_files) entry."""
    clusters: list[tuple[datetime, list[Path]]] = []
    for hit in sorted(hits, key=lambda h: h.timestamp):
        for i, (canonical_ts, paths) in enumerate(clusters):
            if abs((hit.timestamp - canonical_ts).total_seconds()) <= _CLUSTER_TOLERANCE.total_seconds():
                paths.append(hit.source_file)
                break
        else:
            clusters.append((hit.timestamp, [hit.source_file]))
    return clusters


@dataclass
class _WindowStats:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    message_count: int = 0
    session_count: int = 0
    model_distribution: dict = field(default_factory=dict)


def _window_stats(projects_dir: Path, event_time: datetime, window: timedelta) -> _WindowStats:
    """Real token usage in [event_time - window, event_time], across ALL
    transcripts including subagents - unlike event *detection*, subagent
    turns are genuine API calls against the same account quota and must
    count toward usage."""
    cutoff = event_time - window
    stats = _WindowStats()
    session_ids: set[str] = set()

    try:
        candidates = list(projects_dir.rglob("*.jsonl"))
    except OSError:
        return stats

    for path in candidates:
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            continue
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
                    ts = _parse_timestamp(entry.get("timestamp"))
                    if ts is None or not (cutoff <= ts <= event_time):
                        continue
                    message = entry.get("message") or {}
                    usage = message.get("usage")
                    if not usage:
                        continue
                    stats.input_tokens += int(usage.get("input_tokens") or 0)
                    stats.output_tokens += int(usage.get("output_tokens") or 0)
                    stats.cache_creation_tokens += int(usage.get("cache_creation_input_tokens") or 0)
                    stats.cache_read_tokens += int(usage.get("cache_read_input_tokens") or 0)
                    stats.message_count += 1
                    touched = True
                    model = message.get("model")
                    if model:
                        stats.model_distribution[model] = stats.model_distribution.get(model, 0) + 1
        except OSError:
            continue
        if touched:
            session_ids.add(path.stem)

    stats.session_count = len(session_ids)
    return stats


def scan_and_record(
    store: ExhaustionStore,
    projects_dir: Optional[Path] = None,
    window: timedelta = DEFAULT_WINDOW,
) -> int:
    """Looks for new real 429s since the store's last checkpoint, clusters
    concurrent-session duplicates into one event each, computes the
    preceding window's token usage, and records genuinely new events.

    The very first call (no checkpoint yet) scans this machine's entire
    transcript history once; every call after that only re-reads files
    touched since the previous checkpoint, so steady-state cost is small
    even though this runs on the same timer as the regular token count.

    Returns how many new events were added.
    """
    projects_dir = projects_dir or (Path.home() / ".claude" / "projects")
    if not projects_dir.is_dir():
        return 0

    since = store.last_scan_cutoff()
    scan_started_at = datetime.now(timezone.utc)

    hits = _scan_for_hits(projects_dir, since)
    added = 0
    for canonical_ts, source_paths in _cluster_hits(hits):
        if store.has_nearby_event(canonical_ts):
            continue
        stats = _window_stats(projects_dir, canonical_ts, window)
        event = ExhaustionEvent(
            timestamp=canonical_ts,
            window_hours=window.total_seconds() / 3600,
            input_tokens=stats.input_tokens,
            output_tokens=stats.output_tokens,
            cache_creation_tokens=stats.cache_creation_tokens,
            cache_read_tokens=stats.cache_read_tokens,
            message_count=stats.message_count,
            session_count=stats.session_count,
            model_distribution=stats.model_distribution,
            source_files=[str(p) for p in source_paths],
        )
        if store.add_event(event):
            added += 1

    # Advances even when nothing was found, so an all-quiet period doesn't
    # force a full historical re-scan on the next tick.
    store.set_last_scan_cutoff(scan_started_at)
    store.save()
    return added


class AnchorStrategy(Protocol):
    method_name: str

    def compute(self, events: list[ExhaustionEvent]) -> Optional[int]:
        ...


class ConservativeMinAnchorStrategy:
    """The smallest fresh-token count at which a real 429 has ever been
    observed - a conservative floor, chosen so the estimate errs toward
    warning early rather than late. See module docstring for when/how to
    replace this."""

    method_name = "min_observed"

    def compute(self, events: list[ExhaustionEvent]) -> Optional[int]:
        if not events:
            return None
        return min(e.fresh_tokens for e in events)


def confidence_for_sample_count(n: int) -> str:
    """Placeholder thresholds - there is no data yet to justify exact cutoffs,
    only the ordering (more independent samples -> more confidence). Revisit
    once enough real events exist to check how much the anchor actually
    varies between them."""
    if n <= 0:
        return "none"
    if n < 3:
        return "low"
    if n < 6:
        return "medium"
    return "high"


@dataclass(frozen=True)
class EstimatedQuota:
    current_fresh_tokens: int
    anchor_fresh_tokens: Optional[int]
    estimated_used_pct: Optional[float]
    confidence: str
    sample_count: int
    method: str


class EstimatedQuotaModel:
    """Combines the current window's fresh-token count with the stored
    exhaustion history to produce an EstimatedQuota. The anchor strategy is
    injected so it can be swapped (e.g. for a median/percentile approach once
    sample_count >= 3) without changing this class or the widget."""

    def __init__(
        self,
        store: ExhaustionStore,
        anchor_strategy: Optional[AnchorStrategy] = None,
    ) -> None:
        self._store = store
        self._anchor_strategy = anchor_strategy or ConservativeMinAnchorStrategy()

    def estimate(self, current_fresh_tokens: int) -> EstimatedQuota:
        events = self._store.events()
        anchor = self._anchor_strategy.compute(events)
        pct = (current_fresh_tokens / anchor * 100) if anchor else None
        return EstimatedQuota(
            current_fresh_tokens=current_fresh_tokens,
            anchor_fresh_tokens=anchor,
            estimated_used_pct=pct,
            confidence=confidence_for_sample_count(len(events)),
            sample_count=len(events),
            method=self._anchor_strategy.method_name,
        )
