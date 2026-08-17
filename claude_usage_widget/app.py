"""Application entry point."""

from __future__ import annotations

import sys

# Must run before PySide6/Qt is imported: on a scaled display (this machine
# measured at 125%), a pythonw.exe process that hasn't declared its own DPI
# awareness gets Windows' compatibility virtualization applied to it, which
# can leave Qt's rendered layout and the OS's actual mouse hit-testing
# disagreeing about where things are - symptoms observed directly: dragging,
# the close button, and the opacity slider all stopped tracking clicks
# correctly, even though the window's content still rendered and its
# programmatic state (e.g. opacity) still updated fine. Declaring
# Per-Monitor-v2 awareness explicitly, this early, removes the ambiguity
# instead of relying on however Qt's own default handling times out against
# Windows' first-window DPI determination.
if sys.platform == "win32":
    import ctypes

    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        except (AttributeError, OSError):
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass

from PySide6.QtWidgets import QApplication

from .settings_store import APPLICATION, ORGANISATION, SettingsStore
from .usage_source import build_default_provider
from .widget import UsageWidget


def main() -> int:
    app = QApplication(sys.argv)
    app.setOrganizationName(ORGANISATION)
    app.setApplicationName(APPLICATION)

    # The only window is a Qt.Tool with no taskbar entry; without this, closing
    # it would leave the process running with nothing to show.
    app.setQuitOnLastWindowClosed(True)

    widget = UsageWidget(provider=build_default_provider(), store=SettingsStore())
    widget.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
