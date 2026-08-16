"""Application entry point."""

from __future__ import annotations

import sys

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
