"""The floating desktop window."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QMouseEvent
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
from .settings_store import OPACITY_MAX, OPACITY_MIN, SettingsStore, WindowState
from .usage_source import UsageProvider, UsageSnapshot

# Real file I/O now runs on every tick (scanning local transcripts), unlike
# the old in-memory mock - a slower interval is the honest tradeoff.
REFRESH_INTERVAL_MS = 30_000

STYLE = """
#root {
    background-color: #1c2128;
    border: 1px solid #30363d;
    border-radius: 10px;
}
QLabel { color: #c9d1d9; }
#titleLabel { color: #8b949e; font-size: 11px; font-weight: 600; }
#dataBadge {
    color: #d29922;
    border: 1px solid #d29922;
    border-radius: 4px;
    padding: 0px 4px;
    font-size: 9px;
    font-weight: 600;
}
#usedLabel { font-size: 26px; font-weight: 600; }
#usedSuffix { color: #8b949e; font-size: 11px; }
#detailLabel { color: #8b949e; font-size: 10px; }
#estimatedTitleLabel { color: #8b949e; font-size: 11px; font-weight: 600; }
#estimatedBadge {
    color: #a371f7;
    border: 1px solid #a371f7;
    border-radius: 4px;
    padding: 0px 4px;
    font-size: 9px;
    font-weight: 600;
}
#estimatedPctLabel { font-size: 20px; font-weight: 600; }
#estimatedDetailLabel { color: #8b949e; font-size: 10px; }
#estimatedConfidenceLabel { color: #8b949e; font-size: 10px; }
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
    ) -> None:
        super().__init__()
        self._provider = provider
        self._store = store
        self._exhaustion_store = exhaustion_store or ExhaustionStore()
        self._projects_dir = projects_dir
        self._quota_model = EstimatedQuotaModel(self._exhaustion_store)
        self._drag_offset: QPoint | None = None

        # Qt.Tool keeps it out of the taskbar so it behaves like a desktop
        # ornament rather than an app window.
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool)
        self.setWindowTitle("Claude Code Usage")
        self.setFixedSize(250, 232)
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
        self.refresh()

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
        layout.setSpacing(5)

        layout.addLayout(self._build_title_row())
        layout.addLayout(self._build_reading_row())

        self._detail_label = QLabel()
        self._detail_label.setObjectName("detailLabel")
        layout.addWidget(self._detail_label)

        layout.addLayout(self._build_estimated_section())

        layout.addStretch(1)
        layout.addWidget(self._build_controls())

    def _build_title_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(5)

        title = QLabel("CLAUDE CODE USAGE")
        title.setObjectName("titleLabel")
        row.addWidget(title)

        self._data_badge = QLabel()
        self._data_badge.setObjectName("dataBadge")
        self._data_badge.setToolTip(
            "Real token count from this machine's own Claude Code transcripts. "
            "Not an official quota reading - Claude Code does not expose your "
            "actual limit to local tools. See README."
        )
        row.addWidget(self._data_badge)

        row.addStretch(1)

        close = QPushButton("✕")
        close.setObjectName("closeButton")
        close.setFixedSize(16, 16)
        close.setCursor(Qt.PointingHandCursor)
        close.setToolTip("Close")
        close.clicked.connect(self.close)
        row.addWidget(close)

        return row

    def _build_reading_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(4)

        self._used_label = QLabel("--")
        self._used_label.setObjectName("usedLabel")
        self._used_label.setToolTip("Exact token count shown on hover once loaded.")
        row.addWidget(self._used_label)

        suffix = QLabel("tokens")
        suffix.setObjectName("usedSuffix")
        # Sit the word on the numeral's baseline rather than centring it.
        row.addWidget(suffix, 0, Qt.AlignBottom)

        row.addStretch(1)

        return row

    def _build_estimated_section(self) -> QVBoxLayout:
        section = QVBoxLayout()
        section.setSpacing(2)

        title_row = QHBoxLayout()
        title_row.setSpacing(5)
        title = QLabel("ESTIMATED USED")
        title.setObjectName("estimatedTitleLabel")
        title_row.addWidget(title)

        self._estimated_badge = QLabel("ESTIMATED")
        self._estimated_badge.setObjectName("estimatedBadge")
        self._estimated_badge.setToolTip(
            "Inferred from your own local history of real rate-limit hits - "
            "not an official Anthropic quota reading. The anchor is the "
            "smallest fresh-token count at which this account has actually "
            "hit a 429 locally, used as a conservative floor. See README."
        )
        title_row.addWidget(self._estimated_badge)
        title_row.addStretch(1)
        section.addLayout(title_row)

        self._estimated_pct_label = QLabel("--")
        self._estimated_pct_label.setObjectName("estimatedPctLabel")
        section.addWidget(self._estimated_pct_label)

        self._estimated_detail_label = QLabel()
        self._estimated_detail_label.setObjectName("estimatedDetailLabel")
        section.addWidget(self._estimated_detail_label)

        self._estimated_confidence_label = QLabel()
        self._estimated_confidence_label.setObjectName("estimatedConfidenceLabel")
        section.addWidget(self._estimated_confidence_label)

        return section

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
        label.setObjectName("detailLabel")
        opacity_row.addWidget(label)

        self._opacity_slider = QSlider(Qt.Horizontal)
        self._opacity_slider.setRange(OPACITY_MIN, OPACITY_MAX)
        self._opacity_slider.valueChanged.connect(self._on_opacity_changed)
        opacity_row.addWidget(self._opacity_slider, 1)

        self._opacity_label = QLabel()
        self._opacity_label.setObjectName("detailLabel")
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
        snapshot = self._provider.fetch()
        if snapshot is None:
            self._used_label.setText("--")
            self._used_label.setToolTip("")
            self._detail_label.setText("Usage data unavailable")
            self._data_badge.setVisible(False)
            self._render_estimated(self._quota_model.estimate(0))
            return
        self._render(snapshot)

        # Incremental after the first run (see scan_and_record's docstring) -
        # cheap enough to run on the same 30s timer as the token count.
        scan_and_record(self._exhaustion_store, projects_dir=self._projects_dir)
        self._render_estimated(self._quota_model.estimate(snapshot.fresh_tokens))

    def _render(self, snapshot: UsageSnapshot) -> None:
        self._used_label.setText(format_token_count(snapshot.total_tokens))
        cached = snapshot.total_tokens - snapshot.fresh_tokens
        self._used_label.setToolTip(
            f"{snapshot.total_tokens:,} tokens total\n"
            f"{snapshot.fresh_tokens:,} fresh (input/output/new cache writes)\n"
            f"{cached:,} cached (replayed context - far cheaper, not a 1:1 quota hit)"
        )

        detail = snapshot.headline
        if snapshot.detail:
            detail = f"{detail} · {snapshot.detail}"
        self._detail_label.setText(detail)

        self._data_badge.setText(snapshot.badge_text or "")
        self._data_badge.setVisible(bool(snapshot.badge_text))

    def _render_estimated(self, quota: EstimatedQuota) -> None:
        if quota.anchor_fresh_tokens is None:
            self._estimated_pct_label.setText("--")
            self._estimated_detail_label.setText(
                f"Current {format_token_count(quota.current_fresh_tokens)} tokens"
            )
            self._estimated_confidence_label.setText("No exhaustion event recorded yet")
            return

        self._estimated_pct_label.setText(f"~{quota.estimated_used_pct:.0f}%")
        self._estimated_detail_label.setText(
            f"Current {format_token_count(quota.current_fresh_tokens)} "
            f"· Anchor ~{format_token_count(quota.anchor_fresh_tokens)}"
        )
        self._estimated_confidence_label.setText(
            f"Confidence: {quota.confidence.title()} · Samples: {quota.sample_count}"
        )

    def _on_opacity_changed(self, value: int) -> None:
        self.setWindowOpacity(value / 100.0)
        self._opacity_label.setText(f"{value}%")

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

    # ---------- lifecycle ----------

    def closeEvent(self, event) -> None:
        self._store.save(self._current_state())
        super().closeEvent(event)
