"""Headless-ish smoke test: builds the real widget, exercises its behaviour,
then exits. Run with `python selftest.py`.

This drives the actual UI objects (not stand-ins), so it catches wiring
mistakes that a pure unit test of the data layer would miss. It also builds a
throwaway set of synthetic transcript files to test LocalTranscriptUsageProvider
against real scanning/parsing logic, without depending on this machine's own
Claude Code history existing or having any particular shape.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from claude_usage_widget.exhaustion import (
    ConservativeMinAnchorStrategy,
    EstimatedQuotaModel,
    ExhaustionEvent,
    ExhaustionStore,
    confidence_for_sample_count,
    scan_and_record,
)
from claude_usage_widget.rate_limits import (
    OfficialRateLimitProvider,
    OfficialRateLimits,
    RateLimitWindow,
    format_reset_time,
    warning_level,
)
from claude_usage_widget.settings_store import (
    APPLICATION,
    ORGANISATION,
    SettingsStore,
    WindowState,
)
from claude_usage_widget.usage_source import LocalTranscriptUsageProvider, MockUsageProvider
from claude_usage_widget.widget import UsageWidget, format_token_count

failures: list[str] = []

# A distinct QSettings application name for every SettingsStore this suite
# creates - persistence-round-trip tests below deliberately write real
# on/off-screen, opacity, and always-on-top values, and doing that under the
# real APPLICATION name previously left the actual widget's saved window
# state clobbered (e.g. always_on_top=False), so the real app came up
# hidden behind other windows the next time it launched.
TEST_APPLICATION = f"{APPLICATION}-selftest"


def _test_store() -> SettingsStore:
    return SettingsStore(organization=ORGANISATION, application=TEST_APPLICATION)


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        failures.append(label)
        print(f"  FAIL  {label} {detail}")


def _write_transcript(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def _usage_entry(
    ts: datetime,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
) -> dict:
    return {
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "message": {
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": cache_read_tokens,
            }
        },
    }


def test_local_transcript_provider() -> None:
    print("LocalTranscriptUsageProvider (synthetic transcripts)")

    tmp = Path(tempfile.mkdtemp(prefix="usage_widget_selftest_"))
    try:
        now = datetime.now(timezone.utc)
        window = timedelta(hours=5)

        # Session A: one entry inside the window, one well outside it.
        # Only the inside-window entry should be counted.
        session_a = tmp / "projA" / "session-a.jsonl"
        _write_transcript(
            session_a,
            [
                _usage_entry(now - timedelta(hours=1), 100, 200),  # in window
                _usage_entry(now - timedelta(days=2), 999_999, 999_999),  # stale
            ],
        )

        # Session B: entirely inside the window, in a different project dir,
        # to confirm rglob crosses project boundaries and sessions are counted
        # per file (by filename), not merged together.
        session_b = tmp / "projB" / "session-b.jsonl"
        _write_transcript(
            session_b,
            [_usage_entry(now - timedelta(minutes=30), 50, 50)],
        )

        provider = LocalTranscriptUsageProvider(window=window, projects_dir=tmp)
        snap = provider.fetch()

        check("snapshot produced", snap is not None)
        check(
            "only in-window tokens counted (300 + 100 = 400)",
            snap.total_tokens == 400,
            f"got {snap.total_tokens}",
        )
        check("in-window message counted, stale one excluded",
              snap.message_count == 2, f"got {snap.message_count}")
        check("both sessions represented", snap.session_count == 2,
              f"got {snap.session_count}")
        check("no cache_read tokens in this batch -> all counted as fresh",
              snap.fresh_tokens == 400, f"got {snap.fresh_tokens}")
        check("badge marks this as a local estimate, not official",
              snap.badge_text == "LOCAL", f"got {snap.badge_text!r}")
        check("headline mentions the window", "5h" in snap.headline,
              f"got {snap.headline!r}")


        # A file whose mtime predates the window entirely must be skipped by
        # the cheap pre-filter, even before its contents are parsed.
        session_c = tmp / "projC" / "session-c.jsonl"
        _write_transcript(session_c, [_usage_entry(now - timedelta(minutes=5), 77, 77)])
        old_time = (now - timedelta(days=3)).timestamp()
        import os
        os.utime(session_c, (old_time, old_time))

        snap2 = provider.fetch()
        check(
            "mtime pre-filter skips a file whose timestamp predates the window, "
            "without needing to open and parse it",
            snap2.total_tokens == 400,
            f"got {snap2.total_tokens}",
        )

        missing = LocalTranscriptUsageProvider(projects_dir=tmp / "does-not-exist")
        check("missing projects dir returns None", missing.fetch() is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_fresh_vs_cached_split() -> None:
    print("fresh vs cached token split")

    tmp = Path(tempfile.mkdtemp(prefix="usage_widget_selftest_split_"))
    try:
        now = datetime.now(timezone.utc)
        _write_transcript(
            tmp / "proj" / "session.jsonl",
            [_usage_entry(now - timedelta(minutes=10), 10, 20, cache_read_tokens=1_000)],
        )
        snap = LocalTranscriptUsageProvider(projects_dir=tmp).fetch()
        check("cache_read tokens included in the total",
              snap.total_tokens == 1_030, f"got {snap.total_tokens}")
        check("cache_read tokens excluded from fresh_tokens",
              snap.fresh_tokens == 30, f"got {snap.fresh_tokens}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _rate_limit_entry(ts: datetime) -> dict:
    """Shape confirmed against a real local 429 hit: exact typed fields, not
    text a human/tool merely mentions."""
    return {
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "message": {
            "role": "assistant",
            "isApiErrorMessage": True,
            "apiErrorStatus": 429,
            "error": "rate_limit",
            "content": [{"type": "text", "text": "You've hit your session limit"}],
        },
    }


def _fake_echo_entry(ts: datetime, quoted_json_text: str) -> dict:
    """Mimics a tool result that happens to quote rate-limit-shaped JSON as
    plain text (e.g. a research agent's own grep output) - must NOT be
    detected as a real event, since the fields aren't real dict keys here."""
    return {
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "content": quoted_json_text}],
        },
    }


def test_exhaustion_detection_and_window_stats() -> None:
    print("exhaustion detection + window stats (synthetic transcripts)")

    tmp = Path(tempfile.mkdtemp(prefix="usage_widget_selftest_exhaustion_"))
    try:
        now = datetime.now(timezone.utc)
        event_time = now - timedelta(minutes=1)
        usage_before = event_time - timedelta(hours=1)

        # Real hit, with genuine preceding usage in the same file.
        _write_transcript(
            tmp / "projA" / "sess-a.jsonl",
            [_usage_entry(usage_before, 100, 50), _rate_limit_entry(event_time)],
        )
        # Same account-wide 429, logged 10s later by a concurrent process -
        # must cluster into the SAME event, not a second sample.
        _write_transcript(
            tmp / "projB" / "sess-b.jsonl",
            [_rate_limit_entry(event_time + timedelta(seconds=10))],
        )
        # Same error shape, but under a subagents/ directory - must be
        # excluded from detection entirely.
        _write_transcript(
            tmp / "projA" / "session-x" / "subagents" / "agent-1.jsonl",
            [_rate_limit_entry(event_time)],
        )
        # Rate-limit-shaped text quoted inside a tool result's string content,
        # not as real JSON keys - must not be detected either.
        _write_transcript(
            tmp / "projC" / "sess-c.jsonl",
            [_fake_echo_entry(
                event_time,
                '{"error":"rate_limit","isApiErrorMessage":true,"apiErrorStatus":429}',
            )],
        )

        store = ExhaustionStore(path=tmp / "store.json")
        added = scan_and_record(store, projects_dir=tmp, window=timedelta(hours=5))

        check("exactly one clustered event recorded", added == 1, f"got {added}")
        events = store.events()
        check("one event in store", len(events) == 1, f"got {len(events)}")
        if events:
            ev = events[0]
            check("window stats sum only the real usage entry (100+50)",
                  ev.fresh_tokens == 150, f"got {ev.fresh_tokens}")
            check("both concurrent duplicate source files attributed to the event",
                  len(ev.source_files) == 2, f"got {ev.source_files}")

        added_again = scan_and_record(store, projects_dir=tmp, window=timedelta(hours=5))
        check("rescanning the same unchanged files adds nothing new",
              added_again == 0, f"got {added_again}")

        # Reload from disk to confirm persistence round-trips correctly.
        reloaded = ExhaustionStore(path=tmp / "store.json")
        check("event survives a reload from disk", len(reloaded.events()) == 1,
              f"got {len(reloaded.events())}")
        if reloaded.events():
            check("reloaded event has the same fresh_tokens",
                  reloaded.events()[0].fresh_tokens == 150)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_exhaustion_store_dedup() -> None:
    print("ExhaustionStore dedup")

    tmp = Path(tempfile.mkdtemp(prefix="usage_widget_selftest_dedup_"))
    try:
        store = ExhaustionStore(path=tmp / "store.json")
        now = datetime.now(timezone.utc)
        first = ExhaustionEvent(
            timestamp=now, window_hours=5.0, input_tokens=1, output_tokens=1,
            cache_creation_tokens=0, cache_read_tokens=0, message_count=1, session_count=1,
        )
        check("first add succeeds", store.add_event(first) is True)
        nearby = ExhaustionEvent(
            timestamp=now + timedelta(minutes=1), window_hours=5.0, input_tokens=2,
            output_tokens=2, cache_creation_tokens=0, cache_read_tokens=0,
            message_count=1, session_count=1,
        )
        check("a near-duplicate timestamp is rejected as a dedup", store.add_event(nearby) is False)
        check("store still has exactly one event", len(store.events()) == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_confidence_and_anchor() -> None:
    print("confidence thresholds + conservative-minimum anchor")

    check("0 samples -> none", confidence_for_sample_count(0) == "none")
    check("1 sample -> low", confidence_for_sample_count(1) == "low")
    check("2 samples -> low", confidence_for_sample_count(2) == "low")
    check("3 samples -> medium", confidence_for_sample_count(3) == "medium")
    check("5 samples -> medium", confidence_for_sample_count(5) == "medium")
    check("6 samples -> high", confidence_for_sample_count(6) == "high")

    tmp = Path(tempfile.mkdtemp(prefix="usage_widget_selftest_anchor_"))
    try:
        store = ExhaustionStore(path=tmp / "store.json")
        model = EstimatedQuotaModel(store, ConservativeMinAnchorStrategy())

        empty = model.estimate(5_000_000)
        check("no events -> no anchor", empty.anchor_fresh_tokens is None)
        check("no events -> pct is None", empty.estimated_used_pct is None)
        check("no events -> confidence none", empty.confidence == "none")

        now = datetime.now(timezone.utc)
        store.add_event(ExhaustionEvent(
            timestamp=now, window_hours=5.0, input_tokens=8_000_000, output_tokens=0,
            cache_creation_tokens=0, cache_read_tokens=0, message_count=1, session_count=1,
        ))
        store.add_event(ExhaustionEvent(
            timestamp=now - timedelta(days=10), window_hours=5.0, input_tokens=12_000_000,
            output_tokens=0, cache_creation_tokens=0, cache_read_tokens=0,
            message_count=1, session_count=1,
        ))
        quota = model.estimate(6_000_000)
        check("anchor is the smaller of the two observed events (8M, not 12M)",
              quota.anchor_fresh_tokens == 8_000_000, f"got {quota.anchor_fresh_tokens}")
        check("pct computed against the conservative (smaller) anchor",
              quota.estimated_used_pct is not None and abs(quota.estimated_used_pct - 75.0) < 0.01,
              f"got {quota.estimated_used_pct}")
        check("2 samples -> low confidence", quota.confidence == "low")
        check("sample_count reflects stored events", quota.sample_count == 2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_format_token_count() -> None:
    print("format_token_count")
    check("small number unchanged", format_token_count(42) == "42")
    check("thousands as K", format_token_count(12_345) == "12.3K",
          f"got {format_token_count(12_345)!r}")
    check("millions as M", format_token_count(1_284_300) == "1.28M",
          f"got {format_token_count(1_284_300)!r}")


def test_warning_levels() -> None:
    print("warning_level thresholds")
    check("69.9 -> normal", warning_level(69.9) == "normal")
    check("70 -> elevated (boundary)", warning_level(70) == "elevated")
    check("84.9 -> elevated", warning_level(84.9) == "elevated")
    check("85 -> high (boundary)", warning_level(85) == "high")
    check("94.9 -> high", warning_level(94.9) == "high")
    check("95 -> critical (boundary)", warning_level(95) == "critical")
    check("100 -> critical", warning_level(100) == "critical")
    check("0 -> normal", warning_level(0) == "normal")


def test_rate_limit_window_rounding_and_reset() -> None:
    print("RateLimitWindow rounding + reset-time formatting")
    window = RateLimitWindow(
        used_percentage=57.99999999999999,
        resets_at=datetime(2026, 8, 17, 5, 10, 0, tzinfo=timezone.utc),  # 13:10 Taipei (UTC+8)
    )
    check("57.99999999999999 rounds to 58, not truncates to 57",
          window.rounded_percentage == 58, f"got {window.rounded_percentage}")
    check("level derived from used_percentage", window.level == "normal", f"got {window.level}")
    check("reset time converted to Asia/Taipei (UTC+8), HH:MM only",
          window.reset_label() == "13:10", f"got {window.reset_label()!r}")
    check("format_reset_time matches the same conversion",
          format_reset_time(window.resets_at) == "13:10")

    print("RateLimitWindow.time_until_reset")
    reference = datetime(2026, 8, 17, 10, 0, 0, tzinfo=timezone.utc)
    hours_and_minutes = RateLimitWindow(used_percentage=0, resets_at=reference + timedelta(hours=2, minutes=13))
    check("hours + minutes format", hours_and_minutes.time_until_reset(now=reference) == "2h 13m",
          f"got {hours_and_minutes.time_until_reset(now=reference)!r}")
    minutes_only = RateLimitWindow(used_percentage=0, resets_at=reference + timedelta(minutes=45))
    check("minutes-only format when under an hour", minutes_only.time_until_reset(now=reference) == "45m",
          f"got {minutes_only.time_until_reset(now=reference)!r}")
    already_passed = RateLimitWindow(used_percentage=0, resets_at=reference - timedelta(minutes=5))
    check("a reset time already in the past reads 'due now'",
          already_passed.time_until_reset(now=reference) == "due now",
          f"got {already_passed.time_until_reset(now=reference)!r}")


def test_official_rate_limit_provider() -> None:
    print("OfficialRateLimitProvider (capture file parsing)")

    tmp = Path(tempfile.mkdtemp(prefix="usage_widget_selftest_official_"))
    try:
        capture_path = tmp / "official_rate_limits.json"

        missing_provider = OfficialRateLimitProvider(capture_path=capture_path)
        check("missing capture file -> None", missing_provider.fetch() is None)

        capture_path.write_text("not json", encoding="utf-8")
        check("malformed JSON -> None", missing_provider.fetch() is None)

        fresh_payload = {
            "rate_limits": {
                "five_hour": {"used_percentage": 57.99999999999999, "resets_at": 1786943400},
                "seven_day": {"used_percentage": 89, "resets_at": 1786942800},
            },
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
        capture_path.write_text(json.dumps(fresh_payload), encoding="utf-8")
        result = missing_provider.fetch()
        check("fresh, well-formed capture parses", result is not None)
        if result is not None:
            check("five_hour used_percentage matches source, rounds to 58",
                  result.five_hour.rounded_percentage == 58, f"got {result.five_hour.rounded_percentage}")
            check("seven_day used_percentage is 89",
                  result.seven_day.rounded_percentage == 89, f"got {result.seven_day.rounded_percentage}")
            check("overall_percentage is max(five_hour, seven_day), not an average",
                  result.overall_percentage == 89, f"got {result.overall_percentage}")
            check("overall_level reflects the higher (weekly) figure",
                  result.overall_level == "high", f"got {result.overall_level}")

        stale_payload = dict(fresh_payload)
        stale_payload["captured_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=45)
        ).isoformat()
        capture_path.write_text(json.dumps(stale_payload), encoding="utf-8")
        stale_provider = OfficialRateLimitProvider(capture_path=capture_path, max_age=timedelta(minutes=30))
        check("data older than max_age is treated as unavailable, not shown as current",
              stale_provider.fetch() is None)

        # A BOM prepended by some invocation paths (observed via a PowerShell
        # pipe into a native exe) must not break parsing.
        bom_path = tmp / "with_bom.json"
        bom_path.write_bytes(b"\xef\xbb\xbf" + json.dumps(fresh_payload).encode("utf-8"))
        bom_provider = OfficialRateLimitProvider(capture_path=bom_path)
        check("leading UTF-8 BOM is tolerated", bom_provider.fetch() is not None)

        incomplete_payload = {
            "rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": 123}},
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
        incomplete_path = tmp / "incomplete.json"
        incomplete_path.write_text(json.dumps(incomplete_payload), encoding="utf-8")
        incomplete_provider = OfficialRateLimitProvider(capture_path=incomplete_path)
        check("missing seven_day bucket -> None rather than a half-filled reading",
              incomplete_provider.fetch() is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    app = QApplication(sys.argv)
    app.setOrganizationName(ORGANISATION)
    app.setApplicationName(APPLICATION)

    test_format_token_count()
    test_warning_levels()
    test_rate_limit_window_rounding_and_reset()
    test_official_rate_limit_provider()
    test_local_transcript_provider()
    test_fresh_vs_cached_split()
    test_exhaustion_detection_and_window_stats()
    test_exhaustion_store_dedup()
    test_confidence_and_anchor()

    print("provider (mock, for widget wiring checks below)")
    provider = MockUsageProvider()
    snap = provider.fetch()
    check("fetch returns a snapshot", snap is not None)
    check("total_tokens non-negative", snap.total_tokens >= 0, f"got {snap.total_tokens}")
    check("badge marks mock data", snap.badge_text == "MOCK")

    class _FakeOfficialProvider:
        """Test double for OfficialRateLimitProvider - avoids depending on
        (or being thrown off by) this machine's real, live capture file."""
        name = "fake_official"

        def __init__(self) -> None:
            self.result: Optional[OfficialRateLimits] = None

        def fetch(self) -> Optional[OfficialRateLimits]:
            return self.result

    print("widget - no official data, no exhaustion history")
    store = _test_store()
    # Isolated exhaustion store + a non-existent projects_dir - this widget
    # instance must not depend on (or mutate) this machine's real exhaustion
    # history, and must not trigger a real scan against ~/.claude/projects.
    widget_scratch = Path(tempfile.mkdtemp(prefix="usage_widget_selftest_widget_"))
    widget_exhaustion_store = ExhaustionStore(path=widget_scratch / "store.json")
    fake_official = _FakeOfficialProvider()
    widget = UsageWidget(
        provider=provider,
        store=store,
        exhaustion_store=widget_exhaustion_store,
        projects_dir=widget_scratch / "does-not-exist",
        official_provider=fake_official,
    )
    widget.show()
    # The constructor's initial refresh() is deferred via QTimer.singleShot(0, ...)
    # so the window can appear before that (real file I/O) work finishes -
    # a real (not zero-length) wait is needed so the singleShot timer
    # actually gets dispatched before checking its result.
    QTest.qWait(50)

    check("window is visible", widget.isVisible())
    check("compact size", widget.width() <= 320 and widget.height() <= 320,
          f"got {widget.width()}x{widget.height()}")
    # MockUsageProvider's count is a function of elapsed wall-clock time, so
    # comparing against the earlier standalone `snap` fetched above would be
    # flaky now that the widget's own fetch happens after a real qWait delay
    # rather than back-to-back with it. Check self-consistency instead: pull
    # the exact figure out of the tooltip itself and confirm the rounded
    # label matches *that*, rather than a separately-timed reading.
    tooltip = widget._local_label.toolTip()
    tooltip_match = re.search(r"([\d,]+) tokens total", tooltip)
    check("exact count available on hover", tooltip_match is not None, f"got {tooltip!r}")
    if tooltip_match:
        exact_total = int(tooltip_match.group(1).replace(",", ""))
        check("local line shows the same count, just rounded/compacted",
              widget._local_label.text() == f"Local: {format_token_count(exact_total)} tokens",
              f"label={widget._local_label.text()!r} tooltip_total={exact_total}")
    check("hover breaks down fresh vs cached tokens",
          "fresh" in tooltip and "cached" in tooltip)
    check("5H shows -- with no official data and no exhaustion history",
          widget._five_hour_pct_label.text() == "--",
          f"got {widget._five_hour_pct_label.text()!r}")
    check("5H badge hidden when there is nothing to attribute",
          not widget._five_hour_badge.isVisible())
    check("Weekly shows -- (no official data, and estimate can't cover this window)",
          widget._seven_day_pct_label.text() == "--",
          f"got {widget._seven_day_pct_label.text()!r}")

    print("widget - official rate_limits available")
    # Offsets from "now" rather than a fixed calendar date, so the countdown
    # ("Xh Ym left") this test checks for stays correct no matter when the
    # suite actually runs.
    now = datetime.now(timezone.utc)
    five_hour_reset_at = now + timedelta(hours=2, minutes=13)
    seven_day_reset_at = now + timedelta(hours=1, minutes=3)
    fake_official.result = OfficialRateLimits(
        five_hour=RateLimitWindow(used_percentage=57.99999999999999, resets_at=five_hour_reset_at),
        seven_day=RateLimitWindow(used_percentage=89, resets_at=seven_day_reset_at),
        captured_at=now,
    )
    widget.refresh()
    check("5H shows rounded official percentage",
          widget._five_hour_pct_label.text() == "58%", f"got {widget._five_hour_pct_label.text()!r}")
    check("5H reset label shows Taipei clock time",
          f"Reset {format_reset_time(five_hour_reset_at)}" in widget._five_hour_reset_label.text(),
          f"got {widget._five_hour_reset_label.text()!r}")
    check("5H reset label also shows a countdown to reset",
          "left" in widget._five_hour_reset_label.text(),
          f"got {widget._five_hour_reset_label.text()!r}")
    check("5H badge says OFFICIAL",
          widget._five_hour_badge.isVisible() and widget._five_hour_badge.text() == "OFFICIAL")
    check("Weekly shows 89%", widget._seven_day_pct_label.text() == "89%",
          f"got {widget._seven_day_pct_label.text()!r}")
    check("Weekly reset label shows Taipei clock time",
          f"Reset {format_reset_time(seven_day_reset_at)}" in widget._seven_day_reset_label.text(),
          f"got {widget._seven_day_reset_label.text()!r}")
    check("Weekly reset label also shows a countdown to reset",
          "left" in widget._seven_day_reset_label.text(),
          f"got {widget._seven_day_reset_label.text()!r}")
    check("Weekly badge says OFFICIAL",
          widget._seven_day_badge.isVisible() and widget._seven_day_badge.text() == "OFFICIAL")
    check("estimated-only 'Estimated Used' presentation does not appear once official data exists",
          "~" not in widget._five_hour_pct_label.text() and "~" not in widget._seven_day_pct_label.text())

    print("widget - falls back to historical estimate once an exhaustion event exists and official is gone")
    fake_official.result = None
    widget_exhaustion_store.add_event(ExhaustionEvent(
        timestamp=datetime.now(timezone.utc), window_hours=5.0,
        input_tokens=10_000_000, output_tokens=0,
        cache_creation_tokens=0, cache_read_tokens=0, message_count=1, session_count=1,
    ))
    # Drive the fallback render directly with a fixed value rather than
    # through refresh()'s live time-based MockUsageProvider fresh_tokens, so
    # the 50% expected below can't be thrown off by wall-clock timing.
    widget._render_estimate_fallback(widget._quota_model.estimate(5_000_000))
    check("5H falls back to ~50% (current is half the anchor)",
          widget._five_hour_pct_label.text() == "~50%",
          f"got {widget._five_hour_pct_label.text()!r}")
    check("5H badge says ESTIMATED, not OFFICIAL, once official data is gone",
          widget._five_hour_badge.isVisible() and widget._five_hour_badge.text() == "ESTIMATED")
    check("5H detail shows the anchor",
          "Anchor" in widget._five_hour_reset_label.text(),
          f"got {widget._five_hour_reset_label.text()!r}")
    check("Weekly has no historical-estimate equivalent, so it stays --",
          widget._seven_day_pct_label.text() == "--",
          f"got {widget._seven_day_pct_label.text()!r}")
    check("Weekly badge hidden in the fallback state",
          not widget._seven_day_badge.isVisible())
    shutil.rmtree(widget_scratch, ignore_errors=True)

    print("refresh() with no local data and no official data available")
    class _EmptyProvider:
        name = "empty"
        def fetch(self):
            return None
    widget2 = UsageWidget(
        provider=_EmptyProvider(),
        store=_test_store(),
        exhaustion_store=ExhaustionStore(path=Path(tempfile.mkdtemp()) / "store.json"),
        projects_dir=Path(tempfile.mkdtemp()) / "does-not-exist",
        official_provider=_FakeOfficialProvider(),
    )
    widget2.show()
    QTest.qWait(50)  # let the deferred initial refresh() run
    check("shows placeholder when local provider returns None",
          widget2._local_label.text() == "Local: unavailable",
          f"got {widget2._local_label.text()!r}")
    check("5H shows -- when neither official nor estimate is available",
          widget2._five_hour_pct_label.text() == "--")
    widget2.close()

    print("always-on-top toggle")
    widget._on_top_box.setChecked(True)
    check("flag set when enabled", bool(widget.windowFlags() & Qt.WindowStaysOnTopHint))
    check("still visible after flag change", widget.isVisible())
    widget._on_top_box.setChecked(False)
    check("flag cleared when disabled",
          not (widget.windowFlags() & Qt.WindowStaysOnTopHint))
    check("still visible after clearing", widget.isVisible())

    print("opacity slider")
    widget._opacity_slider.setValue(30)
    check("30% applied", abs(widget.windowOpacity() - 0.30) < 0.01,
          f"got {widget.windowOpacity():.2f}")
    check("label tracks slider", widget._opacity_label.text() == "30%",
          f"got {widget._opacity_label.text()!r}")
    widget._opacity_slider.setValue(100)
    check("100% applied", abs(widget.windowOpacity() - 1.0) < 0.01)
    check("slider floor is 30", widget._opacity_slider.minimum() == 30)
    check("slider ceiling is 100", widget._opacity_slider.maximum() == 100)

    print("move + persistence round-trip")
    target = QPoint(340, 260)
    widget.move(target)
    widget._opacity_slider.setValue(72)
    widget._on_top_box.setChecked(True)
    widget.close()

    reloaded = _test_store().load()
    check("position persisted", reloaded.position == target,
          f"got {reloaded.position}")
    check("opacity persisted", reloaded.opacity_pct == 72,
          f"got {reloaded.opacity_pct}")
    check("always-on-top persisted", reloaded.always_on_top is True,
          f"got {reloaded.always_on_top}")

    print("close hides rather than quits (this is what makes reopening fast)")
    check("widget is hidden, not destroyed, after close()", not widget.isVisible())
    check("process is still alive - object wasn't torn down", widget is not None)
    widget.show()
    QTest.qWait(50)  # let showEvent's deferred refresh run
    check("shows again without reconstruction", widget.isVisible())
    check("still has real data after being re-shown (not reset to placeholders)",
          widget._local_label.text() != "Loading…",
          f"got {widget._local_label.text()!r}")

    print("off-screen position is rejected")
    store.save(WindowState(position=QPoint(-9000, -9000), opacity_pct=80,
                           always_on_top=False))
    recovered = UsageWidget(provider=provider, store=_test_store())
    recovered.show()
    check("moved back on-screen", recovered._is_on_screen(recovered.pos()),
          f"landed at {recovered.pos()}")
    recovered.close()

    QTimer.singleShot(0, app.quit)
    app.exec()

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
