"""On-demand installation of the optional table-recognition dependencies.

The packaged application deliberately ships without PyTorch / Transformers:
they are only needed when table recognition is explicitly used. When that
happens for the first time, the wheels are downloaded into ``deps`` next to
the executable in a background thread. The current conversion degrades to
plain text OCR; subsequent conversions can use Table Transformer.

Use ``PDF2WORD_NO_AUTO_DEPS=1`` to disable automatic installation.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Callable, List, Optional

from .app_paths import get_deps_dir

logger = logging.getLogger(__name__)

TORCH_VERSION = "2.5.1"
TRANSFORMERS_VERSION = "4.46.3"
CPU_INDEX = "https://download.pytorch.org/whl/cpu"
PYPI_INDEX = "https://pypi.org/simple"
REQUIRED_MODULES = ("torch", "transformers")

# Installed with --no-deps so a second (possibly ABI-incompatible) numpy is
# never pulled into the app -- the bundled numpy is used instead.
_PACKAGES_CPU: List[str] = [f"torch=={TORCH_VERSION}"]
_PACKAGES_PYPI: List[str] = [
    f"transformers=={TRANSFORMERS_VERSION}",
    "tokenizers==0.20.3",
    "huggingface-hub==0.27.1",
    "safetensors==0.4.5",
    "regex==2024.11.6",
    "pyyaml==6.0.2",
    "filelock==3.16.1",
    "fsspec==2024.10.0",
    "typing-extensions==4.12.2",
    "sympy==1.13.1",
    "mpmath==1.3.0",
    "networkx==3.2.1",
    "jinja2==3.1.4",
    "markupsafe==2.1.5",
    "packaging==24.2",
    "requests==2.32.3",
    "urllib3==2.2.3",
    "charset-normalizer==3.4.0",
    "idna==3.10",
    "tqdm==4.67.1",
]

_state = "unknown"  # unknown | available | installing | failed
_state_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_status_callback: Optional[Callable[[str], None]] = None


def set_status_callback(callback: Optional[Callable[[str], None]]) -> None:
    """Register an optional UI callback for installation progress messages."""
    global _status_callback
    _status_callback = callback


def _notify(message: str) -> None:
    logger.info(message)
    callback = _status_callback
    if callback is not None:
        try:
            callback(message)
        except Exception:  # a broken UI must not break the installer
            logger.debug("Dependency status callback failed", exc_info=True)


def ensure_path() -> None:
    """Make the runtime dependency directory importable."""
    directory = str(get_deps_dir())
    if directory not in sys.path:
        sys.path.insert(0, directory)
        importlib.invalidate_caches()


def available() -> bool:
    """Return True when both optional packages can be imported."""
    global _state
    ensure_path()
    if _state == "available":
        return True
    if _state == "installing":
        return False
    try:
        found = all(
            importlib.util.find_spec(module) is not None for module in REQUIRED_MODULES
        )
    except Exception:  # a partially installed package may raise here
        found = False
    if found:
        _state = "available"
    elif _state != "failed":
        _state = "unknown"
    return found


def request_install() -> bool:
    """Start a background installation if needed.

    Returns True when dependencies are already available, False when an
    installation was started (or is already running). Never raises.
    """
    global _state, _thread
    if os.environ.get("PDF2WORD_NO_AUTO_DEPS") == "1":
        _notify("已跳过表格识别依赖自动下载 (PDF2WORD_NO_AUTO_DEPS=1)")
        return False
    if available():
        return True
    with _state_lock:
        if _state == "installing" and _thread is not None and _thread.is_alive():
            return False
        _state = "installing"
        _thread = threading.Thread(
            target=_install_worker, name="pdf2word-deps", daemon=True
        )
        _thread.start()
        _notify("正在后台下载表格识别依赖，本次转换将表格按文字识别")
        return False


def _pip_base_args(directory: str, log_path: str) -> List[str]:
    return [
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--only-binary=:all:",
        "--no-warn-script-location",
        "--no-deps",
        "--upgrade",
        "--target",
        directory,
        "--log",
        log_path,
    ]


def _run_pip(args: List[str]) -> int:
    """Run pip in-process when bundled, otherwise via the current Python."""
    try:
        from pip._internal.cli.main import main as pip_main
    except Exception:
        return subprocess.call([sys.executable, "-m", "pip"] + args)
    try:
        return int(pip_main(args) or 0)
    except SystemExit as exc:
        return int(exc.code or 0)


def _module_found() -> bool:
    ensure_path()
    try:
        return all(
            importlib.util.find_spec(module) is not None for module in REQUIRED_MODULES
        )
    except Exception:
        return False


def _install_worker() -> None:
    global _state
    directory = get_deps_dir()
    lock_path = directory / ".installing"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # A lock left behind by a crashed process is safe to remove after an hour.
        if lock_path.exists() and time.time() - lock_path.stat().st_mtime > 3600:
            lock_path.unlink()
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            _notify("另一个表格识别依赖安装正在进行，跳过本次下载")
            _state = "failed"
            return
        with os.fdopen(fd, "w") as handle:
            handle.write(str(os.getpid()))

        _notify("正在后台下载 CPU 版 torch（约 200 MB），请稍候")
        log_path = str(directory / "install.log")
        base = _pip_base_args(str(directory), log_path)
        steps = (
            base + ["--index-url", CPU_INDEX] + _PACKAGES_CPU,
            base + ["--index-url", PYPI_INDEX] + _PACKAGES_PYPI,
        )
        for args in steps:
            code = _run_pip(args)
            if code != 0:
                raise RuntimeError(
                    f"pip 安装失败（退出码 {code}），详见 {log_path}"
                )

        ensure_path()
        importlib.invalidate_caches()
        if _module_found():
            _state = "available"
            _notify("表格识别依赖安装完成，下次转换即可使用表格识别")
        else:
            _state = "failed"
            _notify(f"表格识别依赖安装后仍不可用，详见 {log_path}")
    except Exception as exc:
        _state = "failed"
        _notify(f"表格识别依赖安装失败: {exc}")
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass
