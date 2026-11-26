"""Tests for the on-demand table dependency installer (no network access)."""

import threading
import types

import pytest

from pdf2word import table_deps


@pytest.fixture(autouse=True)
def reset_state(monkeypatch, tmp_path):
    monkeypatch.setattr(table_deps, "_state", "unknown")
    monkeypatch.setattr(table_deps, "_thread", None)
    monkeypatch.setattr(table_deps, "_status_callback", None)
    monkeypatch.setattr(table_deps, "get_deps_dir", lambda: tmp_path / "deps")
    monkeypatch.delenv("PDF2WORD_NO_AUTO_DEPS", raising=False)
    yield
    # Do not leave the test dependency folder on sys.path.
    path = str(tmp_path / "deps")
    if path in table_deps.sys.path:
        table_deps.sys.path.remove(path)


def fake_find_spec(found, monkeypatch):
    def _find(name, *args, **kwargs):
        return types.SimpleNamespace(name=name) if found.get(name) else None

    monkeypatch.setattr(table_deps.importlib.util, "find_spec", _find)


def wait_for_thread():
    thread = table_deps._thread
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_available_when_both_packages_found(monkeypatch, tmp_path):
    fake_find_spec({"torch": True, "transformers": True}, monkeypatch)
    assert table_deps.available() is True
    assert table_deps._state == "available"
    assert str(tmp_path / "deps") in table_deps.sys.path


def test_missing_packages_start_background_install(monkeypatch):
    found = {"torch": False, "transformers": False}
    fake_find_spec(found, monkeypatch)
    calls = []

    def fake_pip(args):
        calls.append(args)
        found["torch"] = True
        found["transformers"] = True
        return 0

    monkeypatch.setattr(table_deps, "_run_pip", fake_pip)
    messages = []
    table_deps.set_status_callback(messages.append)

    assert table_deps.request_install() is False
    wait_for_thread()

    assert len(calls) == 2  # CPU torch, then transformers from PyPI
    assert "--target" in calls[0]
    assert table_deps.CPU_INDEX in calls[0]
    assert table_deps.PYPI_INDEX in calls[1]
    assert table_deps.available() is True
    assert "正在后台下载" in "".join(messages)


def test_install_failure_is_reported_but_not_raised(monkeypatch):
    fake_find_spec({"torch": False, "transformers": False}, monkeypatch)
    monkeypatch.setattr(
        table_deps, "_run_pip", lambda args: (_ for _ in ()).throw(OSError("offline"))
    )
    messages = []
    table_deps.set_status_callback(messages.append)

    assert table_deps.request_install() is False
    wait_for_thread()

    assert table_deps._state == "failed"
    assert any("失败" in message for message in messages)


def test_concurrent_requests_share_one_install(monkeypatch):
    fake_find_spec({"torch": False, "transformers": False}, monkeypatch)
    release = threading.Event()

    def fake_pip(args):
        release.wait(timeout=5)
        return 0

    monkeypatch.setattr(table_deps, "_run_pip", fake_pip)

    assert table_deps.request_install() is False
    first = table_deps._thread
    assert table_deps.request_install() is False
    assert table_deps._thread is first
    release.set()
    wait_for_thread()


def test_auto_install_can_be_disabled(monkeypatch, tmp_path):
    fake_find_spec({"torch": False, "transformers": False}, monkeypatch)
    monkeypatch.setenv("PDF2WORD_NO_AUTO_DEPS", "1")
    assert table_deps.request_install() is False
    assert table_deps._thread is None
    assert not (tmp_path / "deps" / ".installing").exists()
