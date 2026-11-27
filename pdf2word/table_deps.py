"""On-demand installation of the optional table-recognition dependencies.

The packaged application deliberately ships without PyTorch / Transformers:
they are only needed when table recognition is explicitly used. When that
happens for the first time, compatible wheels are downloaded directly from
PyPI / the PyTorch CPU index into ``deps`` next to the executable, in a
background thread. That conversion falls back to plain text OCR; subsequent
conversions can use Table Transformer.

No pip, Python installation or import hooks are required: wheels are plain zip
files and are unpacked onto ``sys.path``. ``PDF2WORD_NO_AUTO_DEPS=1`` disables
the whole mechanism.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .app_paths import get_deps_dir

logger = logging.getLogger(__name__)

TORCH_VERSION = "2.5.1"
TRANSFORMERS_VERSION = "4.46.3"
CPU_INDEX = "https://download.pytorch.org/whl/cpu"
PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"
REQUIRED_MODULES = ("torch", "transformers")

# name, version, source (CPU index for torch, PyPI for everything else)
_PACKAGE_SPECS: List[Tuple[str, str, str]] = [
    ("torch", TORCH_VERSION, "cpu"),
    ("transformers", TRANSFORMERS_VERSION, "pypi"),
    ("tokenizers", "0.20.3", "pypi"),
    ("huggingface-hub", "0.27.1", "pypi"),
    ("safetensors", "0.4.5", "pypi"),
    ("timm", "1.0.11", "pypi"),
    ("regex", "2024.11.6", "pypi"),
    ("pyyaml", "6.0.2", "pypi"),
    ("filelock", "3.16.1", "pypi"),
    ("fsspec", "2024.10.0", "pypi"),
    ("typing-extensions", "4.12.2", "pypi"),
    ("sympy", "1.13.1", "pypi"),
    ("mpmath", "1.3.0", "pypi"),
    ("networkx", "3.2.1", "pypi"),
    ("jinja2", "3.1.4", "pypi"),
    ("markupsafe", "2.1.5", "pypi"),
    ("packaging", "24.2", "pypi"),
    ("requests", "2.32.3", "pypi"),
    ("urllib3", "2.2.3", "pypi"),
    ("charset-normalizer", "3.4.0", "pypi"),
    ("idna", "3.10", "pypi"),
    ("tqdm", "4.67.1", "pypi"),
]

# Kept for the optional pip fallback used by source runs.
_PACKAGES_CPU: List[str] = [f"torch=={TORCH_VERSION}"]
_PACKAGES_PYPI: List[str] = [
    f"{name}=={version}" for name, version, source in _PACKAGE_SPECS if source == "pypi"
]

_state = "unknown"  # unknown | available | installing | failed
_state_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_status_callback: Optional[Callable[[str], None]] = None


def import_torch():
    """Import torch lazily, without a static import PyInstaller can follow."""
    return importlib.import_module(REQUIRED_MODULES[0])


def import_transformers():
    """Import transformers lazily, without a static import PyInstaller can follow."""
    return importlib.import_module(REQUIRED_MODULES[1])


def set_status_callback(callback: Optional[Callable[[str], None]]) -> None:
    """Register an optional UI callback for installation progress messages."""
    global _status_callback
    _status_callback = callback


def _notify(message: str) -> None:
    logger.info(message)
    try:
        log_path = get_deps_dir() / "install.log"
        if log_path.parent.exists():
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(message + "\n")
    except Exception:  # logging must never break the installer
        pass
    callback = _status_callback
    if callback is not None:
        try:
            callback(message)
        except Exception:
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
    if _module_found():
        _state = "available"
    elif _state != "failed":
        _state = "unknown"
    return _state == "available"


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


def _module_found() -> bool:
    ensure_path()
    try:
        return all(
            importlib.util.find_spec(module) is not None for module in REQUIRED_MODULES
        )
    except Exception:
        return False


# --------------------------------------------------------------- wheel download


def _compat_tags() -> Tuple[str, str, str]:
    """Python ABI and platform tags for the running interpreter."""
    major, minor = sys.version_info[:2]
    platform_tag = sysconfig.get_platform().replace("-", "_").replace(".", "_")
    return f"cp{major}{minor}", f"cp{major}", platform_tag


def _tag_score(tag: str, options: List[str]) -> int:
    values = tag.split(".")
    for index, option in enumerate(options):
        if option in values:
            return len(options) - index
    return -1


def _wheel_score(filename: str, tags: Tuple[str, str, str]) -> int:
    """Return a compatibility score, or -1 if the wheel is incompatible."""
    stem = filename[:-4] if filename.endswith(".whl") else filename
    parts = stem.split("-")
    if len(parts) < 5:
        return -1
    python_tag, abi_tag, platform_tag = parts[-3:]
    python = _tag_score(python_tag, [tags[0], "py3", "py2.py3", tags[1]])
    abi = _tag_score(abi_tag, [tags[0], "abi3", "none"])
    platform = _tag_score(platform_tag, [tags[2], "any"])
    if min(python, abi, platform) < 0:
        return -1
    return python * 100 + abi * 10 + platform


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "pdf2word-dependency-installer/1.0"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def _select_cpu_wheel(name: str, version: str, tags: Tuple[str, str, str]) -> str:
    index_url = f"{CPU_INDEX}/{name}/"
    html = _fetch(index_url).decode("utf-8", "replace")
    best_url, best_score = "", -1
    for href in re.findall(r'href=["\']([^"\']+\.whl)[^"\']*["\']', html):
        url = urllib.parse.urljoin(index_url, href.split("#", 1)[0])
        filename = urllib.parse.unquote(url.rsplit("/", 1)[-1])
        if not filename.startswith(f"{name}-{version}"):
            continue
        score = _wheel_score(filename, tags)
        if score > best_score:
            best_url, best_score = url, score
    return best_url


def _select_pypi_wheel(name: str, version: str, tags: Tuple[str, str, str]) -> str:
    metadata = json.loads(
        _fetch(PYPI_JSON.format(name=name, version=version)).decode("utf-8")
    )
    best_url, best_score = "", -1
    for item in metadata.get("urls", []):
        if item.get("packagetype") != "bdist_wheel":
            continue
        score = _wheel_score(item.get("filename", ""), tags)
        if score > best_score:
            best_url, best_score = item["url"], score
    return best_url


def _download_wheel(url: str, destination: Path) -> None:
    request = urllib.request.Request(
        url, headers={"User-Agent": "pdf2word-dependency-installer/1.0"}
    )
    with urllib.request.urlopen(request, timeout=120) as response, destination.open(
        "wb"
    ) as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)


def _extract_wheel(wheel: Path, target: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        broken = archive.testzip()
        if broken is not None:
            raise RuntimeError(f"wheel 损坏: {wheel.name} ({broken})")
        archive.extractall(target)
    # Wheels may keep platform modules under <dist>-<ver>.data/{pure,plat}lib.
    for data_dir in sorted(target.glob("*.data")):
        for kind in ("purelib", "platlib"):
            source = data_dir / kind
            if source.is_dir():
                for item in source.iterdir():
                    destination = target / item.name
                    if destination.is_dir():
                        shutil.rmtree(destination)
                    elif destination.exists():
                        destination.unlink()
                    shutil.move(str(item), str(destination))
        shutil.rmtree(data_dir, ignore_errors=True)


def _install_via_wheels(directory: Path) -> None:
    tags = _compat_tags()
    wheel_dir = directory / ".wheels"
    wheel_dir.mkdir(parents=True, exist_ok=True)
    try:
        for name, version, source in _PACKAGE_SPECS:
            _notify(f"正在下载 {name}=={version} ...")
            if source == "cpu":
                url = _select_cpu_wheel(name, version, tags)
            else:
                url = _select_pypi_wheel(name, version, tags)
            if not url:
                raise RuntimeError(
                    f"找不到兼容的 {name}=={version} wheel "
                    f"(Python {tags[0]}, 平台 {tags[2]})"
                )
            wheel = wheel_dir / f"{name.replace('-', '_')}-{version}.whl"
            _download_wheel(url, wheel)
            _extract_wheel(wheel, directory)
    finally:
        shutil.rmtree(wheel_dir, ignore_errors=True)


# ------------------------------------------------------------------- pip fallback


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
    """Run pip in-process; frozen builds must not fall back to a subprocess.

    In a PyInstaller build ``sys.executable`` is the app itself, so
    ``<exe> -m pip`` would only launch a second GUI window and silently do
    nothing. Report a clear error instead and let the conversion degrade.
    """
    try:
        from pip._internal.cli.main import main as pip_main
    except Exception as exc:
        if getattr(sys, "frozen", False):
            raise RuntimeError(
                "无法安装表格识别依赖：当前程序内 pip 不可用"
            ) from exc
        return subprocess.call([sys.executable, "-m", "pip"] + args)
    try:
        return int(pip_main(args) or 0)
    except SystemExit as exc:
        return int(exc.code or 0)


def _install_with_pip(directory: Path) -> None:
    base = _pip_base_args(str(directory), str(directory / "install.log"))
    steps = (
        base + ["--index-url", CPU_INDEX] + _PACKAGES_CPU,
        base + ["--index-url", "https://pypi.org/simple"] + _PACKAGES_PYPI,
    )
    for args in steps:
        code = _run_pip(args)
        if code != 0:
            raise RuntimeError(f"pip 安装失败（退出码 {code}）")


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

        _notify("正在后台下载表格识别依赖（CPU 版 torch 约 200 MB），请稍候")
        try:
            _install_via_wheels(directory)
        except Exception as exc:
            if getattr(sys, "frozen", False):
                raise
            _notify(f"直接下载 wheel 失败，改用 pip: {exc}")
            _install_with_pip(directory)

        if _module_found():
            _state = "available"
            _notify("表格识别依赖安装完成，下次转换即可使用表格识别")
        else:
            _state = "failed"
            _notify(
                f"表格识别依赖安装后仍不可用，详见 {directory / 'install.log'}"
            )
    except Exception as exc:
        _state = "failed"
        _notify(f"表格识别依赖安装失败: {exc}")
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass
