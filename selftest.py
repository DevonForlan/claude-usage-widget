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
    STATUS_LAST_KNOWN,
    STATUS_LAST_KNOWN_EXPIRED,
    STATUS_LIVE,
    LIVE_THRESHOLD,
    OfficialRateLimitProvider,
    OfficialRateLimits,
    RateLimitWindow,
    format_age,
    format_reset_time,
    warning_level,
)
from claude_usage_widget import statusline_hook
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

    print("RateLimitWindow.reset_label(include_date=True) - Case 1")
    # 2026-09-14 13:00 Asia/Taipei is actually a Monday (verified via
    # datetime.strftime - not the "Sun" the request illustrated with), so
    # this asserts the real weekday rather than copying a wrong example.
    weekly_window = RateLimitWindow(
        used_percentage=42,
        resets_at=datetime(2026, 9, 14, 5, 0, 0, tzinfo=timezone.utc),  # 13:00 Taipei
    )
    check("include_date=True adds a fixed-English weekday + MM/DD ahead of HH:MM",
          weekly_window.reset_label(include_date=True) == "Mon 09/14 13:00",
          f"got {weekly_window.reset_label(include_date=True)!r}")
    check("format_reset_time(include_date=True) matches the same conversion",
          format_reset_time(weekly_window.resets_at, include_date=True) == "Mon 09/14 13:00")
    check("include_date defaults to False - 5H's plain HH:MM is unaffected",
          weekly_window.reset_label() == "13:00", f"got {weekly_window.reset_label()!r}")

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

        # A reading captured a long time ago (well past the old 30-minute
        # cutoff) must still be returned as-is - there is no age-based
        # rejection any more (see rate_limits.py module docstring). Per-window
        # validity is decided separately via RateLimitWindow.status(), tested
        # in test_window_status_cases() below.
        old_payload = dict(fresh_payload)
        old_payload["rate_limits"] = {
            "five_hour": {
                "used_percentage": 58,
                "resets_at": (datetime.now(timezone.utc) + timedelta(hours=2)).timestamp(),
            },
            "seven_day": {
                "used_percentage": 89,
                "resets_at": (datetime.now(timezone.utc) + timedelta(days=3)).timestamp(),
            },
        }
        old_payload["captured_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=45)
        ).isoformat()
        capture_path.write_text(json.dumps(old_payload), encoding="utf-8")
        old_provider = OfficialRateLimitProvider(capture_path=capture_path)
        old_result = old_provider.fetch()
        check("a reading 45 minutes old is still returned, not discarded",
              old_result is not None)
        if old_result is not None:
            check("its percentage is still the last known value",
                  old_result.five_hour.rounded_percentage == 58,
                  f"got {old_result.five_hour.rounded_percentage}")

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


def test_statusline_hook_last_good_snapshot() -> None:
    """official_rate_limits.json must behave as a LAST-GOOD SNAPSHOT: a
    statusLine render with no usable rate_limits must never clobber the
    previous good reading (Cases E/F/G), a render with a genuinely new
    reading must still update normally (Case H), and a first-ever render
    with nothing usable must not fabricate a file at all (Case I)."""
    print("statusline_hook - last-good-snapshot behaviour (Cases E/F/G/H/I)")

    tmp = Path(tempfile.mkdtemp(prefix="usage_widget_selftest_hook_"))
    try:
        capture_path = tmp / "official_rate_limits.json"
        log_path = tmp / "statusline_hook.log"

        good_payload = json.dumps({
            "rate_limits": {
                "five_hour": {"used_percentage": 29, "resets_at": 1788081000},
                "seven_day": {"used_percentage": 63, "resets_at": 1788152400},
            },
        })
        statusline_hook._process(good_payload, capture_path=capture_path, log_path=log_path)
        first_write = json.loads(capture_path.read_text(encoding="utf-8"))
        check("initial valid render writes the capture file",
              first_write["rate_limits"]["five_hour"]["used_percentage"] == 29)
        captured_at_after_good = first_write["captured_at"]

        # ---- Case E: next render has no rate_limits key at all ----
        no_key_payload = json.dumps({"model": {"display_name": "Claude"}})
        statusline_hook._process(no_key_payload, capture_path=capture_path, log_path=log_path)
        after_e = json.loads(capture_path.read_text(encoding="utf-8"))
        check("Case E: 5H/Weekly values survive a render with no rate_limits key",
              after_e["rate_limits"]["five_hour"]["used_percentage"] == 29
              and after_e["rate_limits"]["seven_day"]["used_percentage"] == 63,
              f"got {after_e['rate_limits']!r}")
        check("Case E: captured_at is untouched", after_e["captured_at"] == captured_at_after_good,
              f"got {after_e['captured_at']!r}")

        # ---- Case F: rate_limits explicitly null ----
        null_payload = json.dumps({"rate_limits": None})
        statusline_hook._process(null_payload, capture_path=capture_path, log_path=log_path)
        after_f = json.loads(capture_path.read_text(encoding="utf-8"))
        check("Case F: rate_limits=null does not overwrite the last good reading",
              after_f == after_e, f"got {after_f!r}")

        # ---- Case G: rate_limits is an empty object ----
        empty_payload = json.dumps({"rate_limits": {}})
        statusline_hook._process(empty_payload, capture_path=capture_path, log_path=log_path)
        after_g = json.loads(capture_path.read_text(encoding="utf-8"))
        check("Case G: rate_limits={} does not overwrite the last good reading",
              after_g == after_f, f"got {after_g!r}")

        # A partial reading (only one window present/valid) must be rejected
        # the same way - this is the second variant of the bug found during
        # root-cause investigation, not explicitly one of E/F/G but covered
        # by the same fix.
        partial_payload = json.dumps({
            "rate_limits": {"five_hour": {"used_percentage": 40, "resets_at": 1788081000}},
        })
        statusline_hook._process(partial_payload, capture_path=capture_path, log_path=log_path)
        after_partial = json.loads(capture_path.read_text(encoding="utf-8"))
        check("a reading with only one valid window does not overwrite the last good reading",
              after_partial == after_g, f"got {after_partial!r}")

        # ---- Case H: a genuinely new, fully valid reading ----
        new_payload = json.dumps({
            "rate_limits": {
                "five_hour": {"used_percentage": 35, "resets_at": 1788081000},
                "seven_day": {"used_percentage": 65, "resets_at": 1788152400},
            },
        })
        statusline_hook._process(new_payload, capture_path=capture_path, log_path=log_path)
        after_h = json.loads(capture_path.read_text(encoding="utf-8"))
        check("Case H: a new valid reading updates the percentages",
              after_h["rate_limits"]["five_hour"]["used_percentage"] == 35
              and after_h["rate_limits"]["seven_day"]["used_percentage"] == 65,
              f"got {after_h['rate_limits']!r}")
        check("Case H: captured_at advances for a genuinely new reading",
              after_h["captured_at"] != captured_at_after_good,
              f"got {after_h['captured_at']!r}")

        # ---- Case I: first-ever render has nothing usable ----
        fresh_capture_path = tmp / "never_written.json"
        fresh_log_path = tmp / "never_written.log"
        statusline_hook._process(no_key_payload, capture_path=fresh_capture_path, log_path=fresh_log_path)
        check("Case I: no file is fabricated when nothing has ever been captured",
              not fresh_capture_path.exists())

        # ---- diagnostic log sanity check ----
        # One entry per _process() call against `log_path` above, in order:
        # good, Case E, Case F, Case G, partial-window, Case H.
        log_lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
        check("diagnostic log recorded one entry per _process() call",
              len(log_lines) == 6, f"got {len(log_lines)} entries")
        check("log entries never carry payload/prompt content, only rate_limits metadata",
              all(set(entry) == {
                  "timestamp", "has_rate_limits", "five_hour_present", "seven_day_present",
                  "five_hour_valid", "seven_day_valid", "wrote", "skip_reason",
                  "file_existed_before", "file_exists_after",
              } for entry in log_lines))
        check("Case E's log entry recorded a skip reason and did not write",
              log_lines[1]["wrote"] is False and log_lines[1]["skip_reason"] == "no_rate_limits_object",
              f"got {log_lines[1]!r}")
        check("the partial-window render's log entry recorded a skip reason and did not write",
              log_lines[4]["wrote"] is False and log_lines[4]["skip_reason"] == "seven_day_missing_or_invalid",
              f"got {log_lines[4]!r}")
        check("Case H's log entry recorded a write",
              log_lines[5]["wrote"] is True and log_lines[5]["skip_reason"] == "",
              f"got {log_lines[5]!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class _FakeOfficialProvider:
    """Test double for OfficialRateLimitProvider - avoids depending on
    (or being thrown off by) this machine's real, live capture file."""
    name = "fake_official"

    def __init__(self) -> None:
        self.result: Optional[OfficialRateLimits] = None

    def fetch(self) -> Optional[OfficialRateLimits]:
        return self.result


def test_window_status_cases() -> None:
    """'Last Known Official Data' regression cases from the redesign: each
    window's own resets_at - not a fixed capture-age cutoff - decides whether
    its last known reading is still shown."""
    print("Last Known Official Data - Cases A/B/C/D")

    now = datetime.now(timezone.utc)
    captured_30m_ago = now - timedelta(minutes=30)

    # ---- Case A: neither window has reset yet -> both LAST KNOWN ----
    five_hour_a = RateLimitWindow(used_percentage=58, resets_at=now + timedelta(hours=1))
    seven_day_a = RateLimitWindow(used_percentage=89, resets_at=now + timedelta(days=2))
    reading_a = OfficialRateLimits(five_hour=five_hour_a, seven_day=seven_day_a, captured_at=captured_30m_ago)
    check("Case A: 5H not yet reset -> LAST_KNOWN",
          reading_a.five_hour_status(now) == STATUS_LAST_KNOWN,
          f"got {reading_a.five_hour_status(now)!r}")
    check("Case A: Weekly not yet reset -> LAST_KNOWN",
          reading_a.seven_day_status(now) == STATUS_LAST_KNOWN,
          f"got {reading_a.seven_day_status(now)!r}")

    # ---- LIVE vs LAST_KNOWN boundary at LIVE_THRESHOLD ----
    just_inside_live = RateLimitWindow(used_percentage=1, resets_at=now + timedelta(hours=1))
    just_outside_live = RateLimitWindow(used_percentage=1, resets_at=now + timedelta(hours=1))
    check("captured just under LIVE_THRESHOLD -> LIVE",
          just_inside_live.status(now - LIVE_THRESHOLD + timedelta(seconds=1), now) == STATUS_LIVE)
    check("captured just over LIVE_THRESHOLD -> LAST_KNOWN",
          just_outside_live.status(now - LIVE_THRESHOLD - timedelta(seconds=1), now) == STATUS_LAST_KNOWN)

    # ---- Case B: 5H has reset, Weekly has not ----
    five_hour_b = RateLimitWindow(used_percentage=58, resets_at=now - timedelta(minutes=5))
    seven_day_b = RateLimitWindow(used_percentage=89, resets_at=now + timedelta(days=2))
    reading_b = OfficialRateLimits(five_hour=five_hour_b, seven_day=seven_day_b, captured_at=captured_30m_ago)
    check("Case B: 5H reset already passed -> LAST_KNOWN_EXPIRED",
          reading_b.five_hour_status(now) == STATUS_LAST_KNOWN_EXPIRED,
          f"got {reading_b.five_hour_status(now)!r}")
    check("Case B: Weekly reset not yet passed -> LAST_KNOWN, independent of 5H",
          reading_b.seven_day_status(now) == STATUS_LAST_KNOWN,
          f"got {reading_b.seven_day_status(now)!r}")

    # ---- Case C: both windows have reset ----
    five_hour_c = RateLimitWindow(used_percentage=58, resets_at=now - timedelta(minutes=5))
    seven_day_c = RateLimitWindow(used_percentage=89, resets_at=now - timedelta(hours=1))
    reading_c = OfficialRateLimits(five_hour=five_hour_c, seven_day=seven_day_c, captured_at=captured_30m_ago)
    check("Case C: 5H -> LAST_KNOWN_EXPIRED", reading_c.five_hour_status(now) == STATUS_LAST_KNOWN_EXPIRED)
    check("Case C: Weekly -> LAST_KNOWN_EXPIRED", reading_c.seven_day_status(now) == STATUS_LAST_KNOWN_EXPIRED)

    # ---- Case D: no official reading has ever been captured ----
    missing_provider = OfficialRateLimitProvider(capture_path=Path(tempfile.mkdtemp()) / "does-not-exist.json")
    check("Case D: no capture file at all -> fetch() is None (HistoricalEstimateProvider is allowed to run)",
          missing_provider.fetch() is None)

    # ---- Same cases, rendered through the actual widget UI ----
    widget_scratch = Path(tempfile.mkdtemp(prefix="usage_widget_selftest_status_"))
    fake_official = _FakeOfficialProvider()
    widget = UsageWidget(
        provider=MockUsageProvider(),
        store=_test_store(),
        exhaustion_store=ExhaustionStore(path=widget_scratch / "store.json"),
        projects_dir=widget_scratch / "does-not-exist",
        official_provider=fake_official,
    )
    widget.show()
    QTest.qWait(50)

    fake_official.result = reading_a
    widget.refresh()
    check("Case A UI: 5H shows the last known 58%",
          widget._five_hour_pct_label.text() == "58%", f"got {widget._five_hour_pct_label.text()!r}")
    check("Case A UI: 5H badge reads LAST KNOWN",
          widget._five_hour_badge.text() == "OFFICIAL · LAST KNOWN",
          f"got {widget._five_hour_badge.text()!r}")
    check("Case A UI: Weekly shows the last known 89%",
          widget._seven_day_pct_label.text() == "89%", f"got {widget._seven_day_pct_label.text()!r}")
    check("Case A UI: Weekly badge reads LAST KNOWN",
          widget._seven_day_badge.text() == "OFFICIAL · LAST KNOWN",
          f"got {widget._seven_day_badge.text()!r}")

    fake_official.result = reading_b
    widget.refresh()
    check("Case B UI: 5H keeps showing its last known 58% - never blanked, never a fake 0%",
          widget._five_hour_pct_label.text() == "58%",
          f"got {widget._five_hour_pct_label.text()!r}")
    check("Case B UI: 5H badge still reads OFFICIAL · LAST KNOWN (not LIVE - this number is stale)",
          widget._five_hour_badge.text() == "OFFICIAL · LAST KNOWN",
          f"got {widget._five_hour_badge.text()!r}")
    check("Case B UI: 5H reset line says awaiting refresh, not a stale/misleading reset time",
          "Awaiting refresh" in widget._five_hour_reset_label.text()
          and "Reset" not in widget._five_hour_reset_label.text(),
          f"got {widget._five_hour_reset_label.text()!r}")
    check("Case B UI: Weekly is unaffected and still shows its last known 89%",
          widget._seven_day_pct_label.text() == "89%", f"got {widget._seven_day_pct_label.text()!r}")
    check("Case B UI: Weekly badge stays LAST KNOWN",
          widget._seven_day_badge.text() == "OFFICIAL · LAST KNOWN",
          f"got {widget._seven_day_badge.text()!r}")

    fake_official.result = reading_c
    widget.refresh()
    check("Case C UI: 5H still shows its last known 58%",
          widget._five_hour_pct_label.text() == "58%",
          f"got {widget._five_hour_pct_label.text()!r}")
    check("Case C UI: Weekly still shows its last known 89%",
          widget._seven_day_pct_label.text() == "89%",
          f"got {widget._seven_day_pct_label.text()!r}")
    check("Case C UI: both reset lines say awaiting refresh",
          "Awaiting refresh" in widget._five_hour_reset_label.text()
          and "Awaiting refresh" in widget._seven_day_reset_label.text(),
          f"got {widget._five_hour_reset_label.text()!r} / {widget._seven_day_reset_label.text()!r}")
    check("Case C UI: neither window fabricates an 'ESTIMATED ~0%' reading",
          "~" not in widget._five_hour_pct_label.text() and "~" not in widget._seven_day_pct_label.text())
    check("Case C UI: badges do not say ESTIMATED (official history still exists, just awaiting refresh)",
          widget._five_hour_badge.text() != "ESTIMATED" and widget._seven_day_badge.text() != "ESTIMATED")

    # ---- Case 4: a fresh official capture arrives after LAST_KNOWN_EXPIRED -> back to LIVE ----
    five_hour_d = RateLimitWindow(used_percentage=3, resets_at=now + timedelta(hours=5))
    seven_day_d = RateLimitWindow(used_percentage=89, resets_at=now + timedelta(days=2))
    fake_official.result = OfficialRateLimits(five_hour=five_hour_d, seven_day=seven_day_d, captured_at=now)
    widget.refresh()
    check("Case 4: a fresh capture updates 5H to the new percentage",
          widget._five_hour_pct_label.text() == "3%", f"got {widget._five_hour_pct_label.text()!r}")
    check("Case 4: 5H badge returns to OFFICIAL · LIVE",
          widget._five_hour_badge.text() == "OFFICIAL · LIVE",
          f"got {widget._five_hour_badge.text()!r}")
    check("Case 4: 5H reset line shows a real reset time again, not 'awaiting refresh'",
          "Awaiting refresh" not in widget._five_hour_reset_label.text()
          and "Reset" in widget._five_hour_reset_label.text(),
          f"got {widget._five_hour_reset_label.text()!r}")

    widget.close()
    shutil.rmtree(widget_scratch, ignore_errors=True)


def test_format_age() -> None:
    print("format_age")
    now = datetime(2026, 8, 17, 10, 0, 0, tzinfo=timezone.utc)
    check("under a minute reads 'just now'",
          format_age(now - timedelta(seconds=30), now=now) == "just now",
          f"got {format_age(now - timedelta(seconds=30), now=now)!r}")
    check("minutes-only format", format_age(now - timedelta(minutes=30), now=now) == "30m ago",
          f"got {format_age(now - timedelta(minutes=30), now=now)!r}")
    check("hours + minutes format",
          format_age(now - timedelta(hours=2, minutes=13), now=now) == "2h 13m ago",
          f"got {format_age(now - timedelta(hours=2, minutes=13), now=now)!r}")


def main() -> int:
    app = QApplication(sys.argv)
    app.setOrganizationName(ORGANISATION)
    app.setApplicationName(APPLICATION)

    test_format_token_count()
    test_warning_levels()
    test_rate_limit_window_rounding_and_reset()
    test_official_rate_limit_provider()
    test_statusline_hook_last_good_snapshot()
    test_window_status_cases()
    test_format_age()
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
    check("5H badge says OFFICIAL - LIVE (captured just now)",
          widget._five_hour_badge.isVisible() and widget._five_hour_badge.text() == "OFFICIAL · LIVE")
    check("Weekly shows 89%", widget._seven_day_pct_label.text() == "89%",
          f"got {widget._seven_day_pct_label.text()!r}")
    check("Weekly reset label shows Taipei clock time WITH the weekday+date (5H doesn't need one, Weekly does)",
          f"Reset {format_reset_time(seven_day_reset_at, include_date=True)}" in widget._seven_day_reset_label.text(),
          f"got {widget._seven_day_reset_label.text()!r}")
    check("Weekly reset label also shows a countdown to reset",
          "left" in widget._seven_day_reset_label.text(),
          f"got {widget._seven_day_reset_label.text()!r}")
    check("Weekly badge says OFFICIAL - LIVE (captured just now)",
          widget._seven_day_badge.isVisible() and widget._seven_day_badge.text() == "OFFICIAL · LIVE")
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
    # Checked via _is_always_on_top() (the real OS-level topmost state), not
    # windowFlags() - while shown on Windows this now goes through a direct
    # SetWindowPos call rather than Qt's flag-recreate path (see
    # _set_always_on_top's docstring), specifically to avoid a visible
    # flicker on every toggle, so Qt's own cached flag no longer follows it.
    widget._on_top_box.setChecked(True)
    check("on-top set when enabled", widget._is_always_on_top())
    check("still visible after toggling on", widget.isVisible())
    widget._on_top_box.setChecked(False)
    check("on-top cleared when disabled", not widget._is_always_on_top())
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
