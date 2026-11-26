"""Resolve user-editable files next to the application executable."""

from __future__ import annotations

import os
import sys
from pathlib import Path

CONFIG_FILENAME = "config.json"
HISTORY_FILENAME = "history.db"
DEPS_DIRNAME = "deps"
APP_DIR_ENV = "PDF2WORD_APP_DIR"


def get_app_dir() -> Path:
    """Directory that holds config.json and history.db.

    PyInstaller builds use the directory containing the executable. Source
    runs use the project root. The environment variable override is mainly
    useful for tests and for portable installations.
    """
    override = os.environ.get(APP_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def get_config_path() -> Path:
    return get_app_dir() / CONFIG_FILENAME


def get_history_path() -> Path:
    return get_app_dir() / HISTORY_FILENAME


def get_deps_dir() -> Path:
    """Directory for optional dependencies installed at runtime.

    It sits next to the executable for packaged builds (next to the project
    root for source runs), so it is writable in normal installations and can
    be backed up together with the app.
    """
    return get_app_dir() / DEPS_DIRNAME
