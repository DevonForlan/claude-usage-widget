"""The floating desktop window.

Data priority for the two big percentages (5H / Weekly):

1. OfficialRateLimitProvider - Anthropic's own rate_limits, captured from
   Claude Code's status line (see rate_limits.py). Used whenever a fresh
   reading exists.
2. HistoricalEstimateProvider (EstimatedQuotaModel, exhaustion.py) - only
   consulted when (1) is unavailable. It only ever covers a 5-hour-shaped
   window (the exhaustion anchor's own scope), so it fills the 5H slot and
   leaves Weekly blank rather than inventing a number that source cannot
   support.
3. LocalTranscriptUsageProvider - never a percentage source; always shown
   separately as a plain token count ("Local: 13.70M tokens"), since it
   answers a different question (how much was used) than the two above
   (what fraction of a limit was used).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QAbstractNativeEventFilter, QPoint, Qt, QTimer
from PySide6.QtGui import QMouseEvent
from PySide6.QtNetwork import QHostAddress, QTcpServer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .exhaustion import EstimatedQuota, EstimatedQuotaModel, ExhaustionStore, scan_and_record
from .rate_limits import OfficialRateLimitProvider, OfficialRateLimits, warning_level
from .settings_store import OPACITY_MAX, OPACITY_MIN, SettingsStore, WindowState
from .usage_source import UsageProvider, UsageSnapshot

# Real file I/O now runs on every tick (scanning local transcripts), unlike
# the old in-memory mock - a slower interval is the honest tradeoff.
REFRESH_INTERVAL_MS = 30_000

# Waking the widget through launcher.exe (see below) costs ~400-500ms no
# matter how fast its own logic is, because that time is almost entirely
# spent starting a brand new .NET process and JIT-warming its first calls
# into Win32/networking - not anything this project's code controls. The
# only way to actually avoid that cost is to avoid spawning a new process at
# all: while the widget is already running (which is most of the time, since
# it hides rather than quits - see closeEvent), it registers this hotkey
# itself via RegisterHotKey and reacts to WM_HOTKEY directly, so pressing it
# doesn't touch launcher.exe or the OS process-creation path in any way.
# launcher.exe (and its Start Menu shortcut, deliberately left without a
# Hotkey of its own now) remains the way to cold-start the widget the first
# time - see the project's autostart-at-login shortcut for how that first
# start happens automatically instead of needing a manual double-click.
WIN_MOD_ALT = 0x0001
WIN_MOD_CONTROL = 0x0002
WIN_MOD_SHIFT = 0x0004
WIN_MOD_NOREPEAT = 0x4000
WIN_VK_U = 0x55
WIN_WM_HOTKEY = 0x0312
HOTKEY_ID = 1


class _HotkeyEventFilter(QAbstractNativeEventFilter):
    """Native filter that catches this process's own WM_HOTKEY message.

    installNativeEventFilter() does not keep a Python reference alive on its
    own - the caller must hold one for as long as the filter should stay
    installed (see UsageWidget._hotkey_filter).
    """

    def __init__(self, callback) -> None:
        super().__init__()
        self._callback = callback

    def nativeEventFilter(self, eventType, message):
        if eventType == b"windows_generic_MSG":
            import ctypes
            from ctypes import wintypes

            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WIN_WM_HOTKEY and msg.wParam == HOTKEY_ID:
                self._callback()
        return False, 0


# The external launcher (launcher/Launcher.cs) used to wake a hidden window
# by calling Win32 ShowWindow()/SetForegroundWindow() directly on its native
# handle. That bypasses QWidget.show()/activateWindow() entirely, and Qt was
# left believing the window was still hidden - it kept repainting and letting
# opacity changes through (those are lower-level, window-manager-owned
# properties), but stopped routing mouse press/move/release events to any
# widget at all, breaking dragging, the close button, and the opacity slider
# simultaneously. The fix is to never let an external process touch the
# native handle - instead the launcher connects to this loopback socket, and
# this process reacts to that connection by calling
# show()/raise()/activateWindow() itself, so Qt runs its own real show path
# and its internal state stays consistent. A plain TCP connect on localhost
# is a push notification handled the moment Qt's event loop next turns, which
# is faster and simpler than the file-mtime-polling this replaced (that had
# to wait for the next poll tick, up to WAKE_POLL_INTERVAL_MS late).
WAKE_PORT = 51823

# Warning-level -> text colour for the big percentage numbers. Each number is
# coloured by ITS OWN value, independently of the other window - a calm 5H
# reading should still look calm even if the weekly figure is critical.
_LEVEL_COLORS = {
    "normal": "#3fb950",
    "elevated": "#d29922",
    "high": "#db6d28",
    "critical": "#f85149",
}

STYLE = """
#root {
    background-color: #1c2128;
    border: 1px solid #30363d;
    border-radius: 10px;
}
QLabel { color: #c9d1d9; }
#titleLabel { color: #8b949e; font-size: 11px; font-weight: 600; }
#sourceBadge {
    color: #3fb950;
    border: 1px solid #3fb950;
    border-radius: 4px;
    padding: 0px 4px;
    font-size: 9px;
    font-weight: 600;
}
#sourceBadge[estimated="true"] {
    color: #a371f7;
    border-color: #a371f7;
}
#windowLabel { color: #8b949e; font-size: 11px; font-weight: 600; }
#pctLabel { font-size: 26px; font-weight: 600; }
#resetLabel { color: #8b949e; font-size: 10px; }
#localLabel { color: #8b949e; font-size: 10px; }
#closeButton {
    color: #8b949e;
    border: none;
    background: transparent;
    font-size: 14px;
}
#closeButton:hover { color: #f85149; }
QCheckBox { color: #8b949e; font-size: 10px; }
QSlider::groove:horizontal {
    background: #30363d;
    height: 4px;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    background: #8b949e;
    width: 10px;
    margin: -4px 0;
    border-radius: 5px;
}
QSlider::handle:horizontal:hover { background: #c9d1d9; }
"""


def format_token_count(n: int) -> str:
    """Compact display for potentially large token counts, e.g. 1284300 -> '1.28M'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


class UsageWidget(QWidget):
    def __init__(
        self,
        provider: UsageProvider,
        store: SettingsStore,
        exhaustion_store: Optional[ExhaustionStore] = None,
        projects_dir: Optional[Path] = None,
        official_provider: Optional[OfficialRateLimitProvider] = None,
    ) -> None:
        super().__init__()
        # This is the LOCAL token count source only now (see module
        # docstring) - kept as the constructor's `provider` param name for
        # compatibility with app.py/selftest.py.
        self._local_provider = provider
        self._official_provider = official_provider or OfficialRateLimitProvider()
        self._store = store
        self._exhaustion_store = exhaustion_store or ExhaustionStore()
        self._projects_dir = projects_dir
        self._quota_model = EstimatedQuotaModel(self._exhaustion_store)
        self._drag_offset: QPoint | None = None

        # Qt.Tool keeps it out of the taskbar so it behaves like a desktop
        # ornament rather than an app window.
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool)
        self.setWindowTitle("Claude Code Usage")
        self.setFixedSize(250, 268)
        self.setStyleSheet(STYLE)

        # #root below draws rounded corners, but this top-level widget is
        # still a plain rectangle behind it. Without this, the 4 corners
        # outside the rounded rect show through as a solid white edge instead
        # of blending into the desktop.
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self._build_ui()

        state = self._store.load()
        self._apply_state(state)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(REFRESH_INTERVAL_MS)

        self._wake_server = QTcpServer(self)
        self._wake_server.newConnection.connect(self._on_wake_connection)
        self._wake_server.listen(QHostAddress.LocalHost, WAKE_PORT)

        self._register_global_hotkey()

        # refresh() does real file I/O (transcript scan + exhaustion scan) -
        # calling it here directly would block the window from appearing at
        # all until that finishes. Deferring to the next event-loop tick lets
        # the window show up instantly with its placeholder state, then fill
        # in real data a moment later instead of delaying first paint.
        QTimer.singleShot(0, self.refresh)

    # ---------- construction ----------

    def _build_ui(self) -> None:
        root = QWidget(self)
        root.setObjectName("root")
        root.setFixedSize(self.size())

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(root)

        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 9, 12, 10)
        layout.setSpacing(6)

        layout.addLayout(self._build_title_row())

        (five_hour_layout, self._five_hour_badge, self._five_hour_pct_label,
         self._five_hour_reset_label) = self._build_window_section("5H")
        layout.addLayout(five_hour_layout)

        (seven_day_layout, self._seven_day_badge, self._seven_day_pct_label,
         self._seven_day_reset_label) = self._build_window_section("WEEKLY")
        layout.addLayout(seven_day_layout)

        self._local_label = QLabel("Loading…")
        self._local_label.setObjectName("localLabel")
        self._local_label.setToolTip("Exact token count shown on hover once loaded.")
        layout.addWidget(self._local_label)

        layout.addStretch(1)
        layout.addWidget(self._build_controls())

    def _build_title_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(5)

        title = QLabel("CLAUDE CODE USAGE")
        title.setObjectName("titleLabel")
        row.addWidget(title)

        row.addStretch(1)

        close = QPushButton("✕")
        close.setObjectName("closeButton")
        close.setFixedSize(16, 16)
        close.setCursor(Qt.PointingHandCursor)
        close.setToolTip("Close")
        close.clicked.connect(self.close)
        row.addWidget(close)

        return row

    def _build_window_section(self, label_text: str):
        section = QVBoxLayout()
        section.setSpacing(1)

        label_row = QHBoxLayout()
        label_row.setSpacing(5)

        label = QLabel(label_text)
        label.setObjectName("windowLabel")
        label_row.addWidget(label)

        badge = QLabel()
        badge.setObjectName("sourceBadge")
        label_row.addWidget(badge)
        label_row.addStretch(1)
        section.addLayout(label_row)

        pct_label = QLabel("--")
        pct_label.setObjectName("pctLabel")
        section.addWidget(pct_label)

        reset_label = QLabel()
        reset_label.setObjectName("resetLabel")
        section.addWidget(reset_label)

        return section, badge, pct_label, reset_label

    def _build_controls(self) -> QWidget:
        holder = QWidget()
        box = QVBoxLayout(holder)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(3)

        self._on_top_box = QCheckBox("Always on top")
        self._on_top_box.toggled.connect(self._on_always_on_top_toggled)
        box.addWidget(self._on_top_box)

        opacity_row = QHBoxLayout()
        opacity_row.setSpacing(6)

        label = QLabel("Opacity")
        label.setObjectName("localLabel")
        opacity_row.addWidget(label)

        self._opacity_slider = QSlider(Qt.Horizontal)
        self._opacity_slider.setRange(OPACITY_MIN, OPACITY_MAX)
        self._opacity_slider.valueChanged.connect(self._on_opacity_changed)
        opacity_row.addWidget(self._opacity_slider, 1)

        self._opacity_label = QLabel()
        self._opacity_label.setObjectName("localLabel")
        self._opacity_label.setFixedWidth(28)
        self._opacity_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        opacity_row.addWidget(self._opacity_label)

        box.addLayout(opacity_row)
        return holder

    # ---------- state ----------

    def _apply_state(self, state: WindowState) -> None:
        self._on_top_box.setChecked(state.always_on_top)
        self._opacity_slider.setValue(state.opacity_pct)

        # Applied directly too: if the slider value happens to equal the
        # default, valueChanged never fires and the window would stay opaque.
        self._on_opacity_changed(state.opacity_pct)
        self._set_always_on_top(state.always_on_top)

        if state.position is not None and self._is_on_screen(state.position):
            self.move(state.position)
        else:
            self._move_to_default_corner()

    def _current_state(self) -> WindowState:
        return WindowState(
            position=self.pos(),
            opacity_pct=self._opacity_slider.value(),
            always_on_top=self._on_top_box.isChecked(),
        )

    def _is_on_screen(self, position: QPoint) -> bool:
        """Guard against a position saved on a monitor that is now gone."""
        probe = QPoint(position.x() + self.width() // 2, position.y() + 12)
        return any(
            screen.availableGeometry().contains(probe)
            for screen in QApplication.screens()
        )

    def _move_to_default_corner(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(area.right() - self.width() - 24, area.top() + 24)

    # ---------- behaviour ----------

    def refresh(self) -> None:
        official = self._official_provider.fetch()
        local_snapshot = self._local_provider.fetch()

        # Incremental after the first run (see scan_and_record's docstring) -
        # cheap enough to run on the same 30s timer as the token count.
        scan_and_record(self._exhaustion_store, projects_dir=self._projects_dir)

        if official is not None:
            self._render_official(official)
        else:
            fresh_tokens = local_snapshot.fresh_tokens if local_snapshot else 0
            estimate = self._quota_model.estimate(fresh_tokens)
            self._render_estimate_fallback(estimate)

        self._render_local(local_snapshot)

    def _render_official(self, official: OfficialRateLimits) -> None:
        self._set_window_section(
            self._five_hour_badge, self._five_hour_pct_label, self._five_hour_reset_label,
            pct_text=f"{official.five_hour.rounded_percentage}%",
            level=official.five_hour.level,
            reset_text=(
                f"Reset {official.five_hour.reset_label()} "
                f"· {official.five_hour.time_until_reset()} left"
            ),
            badge_text="OFFICIAL",
            estimated=False,
        )
        self._set_window_section(
            self._seven_day_badge, self._seven_day_pct_label, self._seven_day_reset_label,
            pct_text=f"{official.seven_day.rounded_percentage}%",
            level=official.seven_day.level,
            reset_text=(
                f"Reset {official.seven_day.reset_label()} "
                f"· {official.seven_day.time_until_reset()} left"
            ),
            badge_text="OFFICIAL",
            estimated=False,
        )

    def _render_estimate_fallback(self, estimate: EstimatedQuota) -> None:
        if estimate.anchor_fresh_tokens is None:
            self._set_window_section(
                self._five_hour_badge, self._five_hour_pct_label, self._five_hour_reset_label,
                pct_text="--", level="normal", reset_text="No official data or estimate yet",
                badge_text="", estimated=False,
            )
        else:
            pct = estimate.estimated_used_pct or 0.0
            self._set_window_section(
                self._five_hour_badge, self._five_hour_pct_label, self._five_hour_reset_label,
                pct_text=f"~{pct:.0f}%",
                level=warning_level(pct),
                reset_text=(
                    f"Anchor {format_token_count(estimate.anchor_fresh_tokens)} · "
                    f"{estimate.confidence.title()} confidence ({estimate.sample_count} samples)"
                ),
                badge_text="ESTIMATED",
                estimated=True,
            )
        # No 7-day-shaped local signal exists (the exhaustion anchor is
        # scoped to a 5-hour window) - showing a fabricated weekly estimate
        # would misrepresent what this fallback can actually support.
        self._set_window_section(
            self._seven_day_badge, self._seven_day_pct_label, self._seven_day_reset_label,
            pct_text="--", level="normal", reset_text="Unavailable without official data",
            badge_text="", estimated=False,
        )

    def _set_window_section(
        self,
        badge: QLabel,
        pct_label: QLabel,
        reset_label: QLabel,
        *,
        pct_text: str,
        level: str,
        reset_text: str,
        badge_text: str,
        estimated: bool,
    ) -> None:
        pct_label.setText(pct_text)
        color = _LEVEL_COLORS.get(level, _LEVEL_COLORS["normal"])
        pct_label.setStyleSheet(f"color: {color};")
        pct_label.setToolTip(f"{level.title()} usage level")
        reset_label.setText(reset_text)

        badge.setText(badge_text)
        badge.setVisible(bool(badge_text))
        badge.setProperty("estimated", "true" if estimated else "false")
        badge.style().unpolish(badge)
        badge.style().polish(badge)

    def _render_local(self, snapshot: Optional[UsageSnapshot]) -> None:
        if snapshot is None:
            self._local_label.setText("Local: unavailable")
            self._local_label.setToolTip("")
            return

        self._local_label.setText(f"Local: {format_token_count(snapshot.total_tokens)} tokens")
        cached = snapshot.total_tokens - snapshot.fresh_tokens
        self._local_label.setToolTip(
            f"{snapshot.headline}\n"
            f"{snapshot.total_tokens:,} tokens total\n"
            f"{snapshot.fresh_tokens:,} fresh (input/output/new cache writes)\n"
            f"{cached:,} cached (replayed context - far cheaper, not a 1:1 quota hit)\n"
            f"{snapshot.detail}"
        )

    def _on_opacity_changed(self, value: int) -> None:
        self.setWindowOpacity(value / 100.0)
        self._opacity_label.setText(f"{value}%")
        # Belt-and-suspenders alongside showEvent's repaint: force the handle
        # to redraw on every change too, in case the staleness isn't only a
        # one-time thing from being shown externally but recurs during live
        # dragging as well.
        self._opacity_slider.update()

    def _on_always_on_top_toggled(self, enabled: bool) -> None:
        self._set_always_on_top(enabled)

    def _set_always_on_top(self, enabled: bool) -> None:
        # Changing this flag recreates the native window, so it must be shown
        # again - and only if it was already visible, or we would pop up a
        # window during construction.
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, enabled)
        if was_visible:
            self.show()

    # ---------- dragging ----------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.pos()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_offset = None
        event.accept()

    # ---------- external wake-up ----------

    def _register_global_hotkey(self) -> None:
        if sys.platform != "win32":
            return
        import ctypes

        # Keep the filter alive on self - installNativeEventFilter() does
        # not hold a reference of its own, and a garbage-collected filter
        # object crashes the process the next time Qt tries to call it.
        self._hotkey_filter = _HotkeyEventFilter(self._on_global_hotkey)
        QApplication.instance().installNativeEventFilter(self._hotkey_filter)

        modifiers = WIN_MOD_CONTROL | WIN_MOD_SHIFT | WIN_MOD_ALT | WIN_MOD_NOREPEAT
        # May return False if something else already owns this combination
        # (e.g. a leftover registration from another instance in the same
        # process during tests) - there is nothing more to do about that
        # from here, so the return value is intentionally not checked.
        ctypes.windll.user32.RegisterHotKey(int(self.winId()), HOTKEY_ID, modifiers, WIN_VK_U)

    def _on_global_hotkey(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _on_wake_connection(self) -> None:
        # The connection itself is the whole signal - nothing needs reading.
        while self._wake_server.hasPendingConnections():
            sock = self._wake_server.nextPendingConnection()
            sock.disconnectFromHost()
            sock.deleteLater()
        self.show()
        self.raise_()
        self.activateWindow()

    # ---------- lifecycle ----------

    def showEvent(self, event) -> None:
        # The window may have sat hidden in the background for a while
        # (see closeEvent) - whatever data it's holding could be stale by the
        # time an external launcher makes it visible again, so refresh as
        # soon as it's shown. Deferred one tick so it doesn't delay the
        # window actually appearing.
        super().showEvent(event)
        QTimer.singleShot(0, self.refresh)

        # The external launcher restores this window via a raw Win32
        # ShowWindow call (see launcher/Launcher.cs), not Qt's own show()
        # path - Qt doesn't always reliably repaint every child widget after
        # that (observed: the opacity slider's handle stayed drawn at its
        # old position even though dragging it still changed the actual
        # opacity correctly - opacity is a layered-window/compositor
        # property, independent of widget-level painting, so it kept
        # working while the handle's stale bitmap did not get refreshed).
        # Forcing a full repaint after any show clears that up.
        self.update()
        self._opacity_slider.update()

    def closeEvent(self, event) -> None:
        # Hide rather than actually quit: PySide6's first window.show() per
        # process costs ~550-650ms on this machine (measured directly, even
        # for a blank QWidget - it's Qt's own first-paint/backend init, not
        # this app's code, so there's nothing to optimize away in Python).
        # Keeping the process alive in the background means that cost is
        # paid once per login, not once per "reopen" - see install.ps1 /
        # launch.ps1, which raise this same hidden instance instead of
        # starting a new one.
        self._store.save(self._current_state())
        event.ignore()
        self.hide()
