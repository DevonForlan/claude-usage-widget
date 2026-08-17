"""Persisted window state (position, opacity, always-on-top).

Backed by QSettings, which on Windows lives in the registry under
HKCU\\Software\\claude-code-toolbox\\claude-usage-widget.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, QSettings

ORGANISATION = "claude-code-toolbox"
APPLICATION = "claude-usage-widget"

OPACITY_MIN = 30
OPACITY_MAX = 100
OPACITY_DEFAULT = 95


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


@dataclass
class WindowState:
    position: QPoint | None
    opacity_pct: int
    always_on_top: bool


class SettingsStore:
    def __init__(self, organization: str = ORGANISATION, application: str = APPLICATION) -> None:
        # Overridable so tests can point at an isolated registry location
        # instead of silently overwriting the real widget's saved window
        # state - this bit selftest.py in practice: its own persistence
        # round-trip tests left always_on_top=False in the real location,
        # so the actual running widget came up hidden behind other windows.
        self._settings = QSettings(organization, application)

    def load(self) -> WindowState:
        pos = self._settings.value("window/position")
        # QSettings hands back whatever type it stored; anything unexpected
        # (or a first run) should fall back to "let Qt place the window".
        position = pos if isinstance(pos, QPoint) else None

        return WindowState(
            position=position,
            opacity_pct=_clamp(
                self._read_int("window/opacity_pct", OPACITY_DEFAULT),
                OPACITY_MIN,
                OPACITY_MAX,
            ),
            always_on_top=self._read_bool("window/always_on_top", True),
        )

    def save(self, state: WindowState) -> None:
        if state.position is not None:
            self._settings.setValue("window/position", state.position)
        self._settings.setValue("window/opacity_pct", int(state.opacity_pct))
        self._settings.setValue("window/always_on_top", bool(state.always_on_top))
        self._settings.sync()

    def _read_int(self, key: str, fallback: int) -> int:
        try:
            return int(self._settings.value(key, fallback))
        except (TypeError, ValueError):
            return fallback

    def _read_bool(self, key: str, fallback: bool) -> bool:
        raw = self._settings.value(key, fallback)
        if isinstance(raw, bool):
            return raw
        # QSettings round-trips booleans through strings on some backends.
        if isinstance(raw, str):
            return raw.strip().lower() in {"true", "1", "yes"}
        return bool(raw)
