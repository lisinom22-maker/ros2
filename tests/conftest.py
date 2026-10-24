"""Make the source-layout package importable with Ubuntu's system pytest."""

import sys
from pathlib import Path

from PyQt5.QtWidgets import QApplication


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE_ROOT))

# Graph tests create their singleton at collection time. Establish the more
# capable application first so they can share an event loop with widget tests.
QT_APPLICATION = QApplication.instance() or QApplication([])
