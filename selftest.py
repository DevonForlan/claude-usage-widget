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
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtWidgets import QApplication

from claude_usage_widget.exhaustion import (
    ConservativeMinAnchorStrategy,
    EstimatedQuotaModel,
    ExhaustionEvent,
    ExhaustionStore,
    confidence_for_sample_count,
    scan_and_record,
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


def main() -> int:
    app = QApplication(sys.argv)
    app.setOrganizationName(ORGANISATION)
    app.setApplicationName(APPLICATION)

    test_format_token_count()
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

    print("widget")
    store = SettingsStore()
    # Isolated exhaustion store + a non-existent projects_dir - this widget
    # instance must not depend on (or mutate) this machine's real exhaustion
    # history, and must not trigger a real scan against ~/.claude/projects.
    widget_scratch = Path(tempfile.mkdtemp(prefix="usage_widget_selftest_widget_"))
    widget_exhaustion_store = ExhaustionStore(path=widget_scratch / "store.json")
    widget = UsageWidget(
        provider=provider,
        store=store,
        exhaustion_store=widget_exhaustion_store,
        projects_dir=widget_scratch / "does-not-exist",
    )
    widget.show()

    check("window is visible", widget.isVisible())
    check("compact size", widget.width() <= 320 and widget.height() <= 320,
          f"got {widget.width()}x{widget.height()}")
    check("token label shows formatted count",
          widget._used_label.text() == format_token_count(snap.total_tokens),
          f"got {widget._used_label.text()!r}")
    check("exact count available on hover",
          f"{snap.total_tokens:,} tokens total" in widget._used_label.toolTip(),
          f"got {widget._used_label.toolTip()!r}")
    check("hover breaks down fresh vs cached tokens",
          "fresh" in widget._used_label.toolTip() and "cached" in widget._used_label.toolTip())
    check("detail line mentions the headline", snap.headline in widget._detail_label.text(),
          f"got {widget._detail_label.text()!r}")
    check("data badge shown with provider's text",
          widget._data_badge.isVisible() and widget._data_badge.text() == "MOCK")
    check("estimated section shows -- with no exhaustion history",
          widget._estimated_pct_label.text() == "--",
          f"got {widget._estimated_pct_label.text()!r}")
    check("estimated confidence line says no event recorded",
          "no exhaustion event" in widget._estimated_confidence_label.text().lower(),
          f"got {widget._estimated_confidence_label.text()!r}")

    print("estimated section once an exhaustion event exists")
    widget_exhaustion_store.add_event(ExhaustionEvent(
        timestamp=datetime.now(timezone.utc), window_hours=5.0,
        input_tokens=10_000_000, output_tokens=0,
        cache_creation_tokens=0, cache_read_tokens=0, message_count=1, session_count=1,
    ))
    # Drives _render_estimated directly with a fixed value rather than going
    # through refresh()/the live time-based MockUsageProvider, so the 50%
    # expected below can't be thrown off by wall-clock timing.
    widget._render_estimated(widget._quota_model.estimate(5_000_000))
    check("estimated pct becomes ~50% (current is half the anchor)",
          widget._estimated_pct_label.text() == "~50%",
          f"got {widget._estimated_pct_label.text()!r}")
    check("estimated detail shows current + anchor",
          "Anchor" in widget._estimated_detail_label.text(),
          f"got {widget._estimated_detail_label.text()!r}")
    check("estimated confidence shows Low / Samples: 1",
          widget._estimated_confidence_label.text() == "Confidence: Low · Samples: 1",
          f"got {widget._estimated_confidence_label.text()!r}")
    shutil.rmtree(widget_scratch, ignore_errors=True)

    print("refresh() with no data available")
    class _EmptyProvider:
        name = "empty"
        def fetch(self):
            return None
    widget2 = UsageWidget(
        provider=_EmptyProvider(),
        store=SettingsStore(),
        exhaustion_store=ExhaustionStore(path=Path(tempfile.mkdtemp()) / "store.json"),
        projects_dir=Path(tempfile.mkdtemp()) / "does-not-exist",
    )
    widget2.show()
    check("shows placeholder when provider returns None",
          widget2._used_label.text() == "--")
    check("badge hidden when no data", not widget2._data_badge.isVisible())
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

    reloaded = SettingsStore().load()
    check("position persisted", reloaded.position == target,
          f"got {reloaded.position}")
    check("opacity persisted", reloaded.opacity_pct == 72,
          f"got {reloaded.opacity_pct}")
    check("always-on-top persisted", reloaded.always_on_top is True,
          f"got {reloaded.always_on_top}")

    print("off-screen position is rejected")
    store.save(WindowState(position=QPoint(-9000, -9000), opacity_pct=80,
                           always_on_top=False))
    recovered = UsageWidget(provider=provider, store=SettingsStore())
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
