"""Entry script for the PyInstaller Windows build."""

import os
import sys

# A windowed PyInstaller build (console=False) leaves sys.stdout and sys.stderr
# as None. tqdm, logging and several native libraries write to them during a
# conversion, which raises: AttributeError: 'NoneType' object has no attribute
# 'write'. Point them at a real (discarded) stream before importing the app.
if sys.stdout is None or sys.stderr is None:
    _devnull = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = _devnull
    if sys.stderr is None:
        sys.stderr = _devnull

# Check for an existing instance before importing the heavy pdf2word package.
# A second launch only signals the first one to show its window, then exits.
from app_single_instance import SingleInstance

_instance = SingleInstance()
if not _instance.acquire():
    _instance.signal_show()
    sys.exit(0)
_instance.create_show_event()

from pdf2word.gui import main

if __name__ == "__main__":
    main(_instance)
