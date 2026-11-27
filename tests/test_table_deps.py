"""Tests for the on-demand table dependency installer (no network access)."""

import sys
import threading
import types
import zipfile

import pytest

from pdf2word import table_deps


@pytest.fixture(autouse=True)
def reset_state(monkeypatch, tmp_path):
    monkeypatch.setattr(table_deps, "_state", "unknown")
    monkeypatch.setattr(table_deps, "_thread", None)
    monkeypatch.setattr(table_deps, "_status_callback", None)
    monkeypatch.setattr(table_deps, "get_deps_dir", lambda: tmp_path / "deps")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.delenv("PDF2WORD_NO_AUTO_DEPS", raising=False)
    yield
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


def test_missing_packages_start_background_wheel_install(monkeypatch):
    found = {"torch": False, "transformers": False}
    fake_find_spec(found, monkeypatch)
    calls = []

    def fake_wheels(directory):
        calls.append(directory)
        found["torch"] = True
        found["transformers"] = True

    monkeypatch.setattr(table_deps, "_install_via_wheels", fake_wheels)
    messages = []
    table_deps.set_status_callback(messages.append)

    assert table_deps.request_install() is False
    wait_for_thread()

    assert len(calls) == 1
    assert table_deps.available() is True
    assert "正在后台下载" in "".join(messages)


def test_wheel_failure_falls_back_to_pip_in_source_run(monkeypatch):
    found = {"torch": False, "transformers": False}
    fake_find_spec(found, monkeypatch)

    def fail_wheels(directory):
        raise RuntimeError("no compatible wheel")

    monkeypatch.setattr(table_deps, "_install_via_wheels", fail_wheels)
    calls = []

    def fake_pip(args):
        calls.append(args)
        found["torch"] = True
        found["transformers"] = True
        return 0

    monkeypatch.setattr(table_deps, "_run_pip", fake_pip)

    assert table_deps.request_install() is False
    wait_for_thread()

    assert len(calls) == 2
    assert table_deps.CPU_INDEX in calls[0]
    assert "https://pypi.org/simple" in calls[1]
    assert table_deps.available() is True


def test_frozen_failure_does_not_retry_with_pip(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    fake_find_spec({"torch": False, "transformers": False}, monkeypatch)

    def fail_wheels(directory):
        raise RuntimeError("offline")

    monkeypatch.setattr(table_deps, "_install_via_wheels", fail_wheels)
    pip_calls = []
    monkeypatch.setattr(table_deps, "_run_pip", lambda args: pip_calls.append(args) or 0)
    messages = []
    table_deps.set_status_callback(messages.append)

    assert table_deps.request_install() is False
    wait_for_thread()

    assert pip_calls == []
    assert table_deps._state == "failed"
    assert any("失败" in message for message in messages)


def test_frozen_build_without_pip_reports_error(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setitem(sys.modules, "pip._internal.cli.main", None)
    calls = []
    monkeypatch.setattr(table_deps.subprocess, "call", lambda args: calls.append(args) or 0)

    with pytest.raises(RuntimeError, match="pip 不可用"):
        table_deps._run_pip(["install"])

    assert calls == []


def test_source_run_falls_back_to_python_m_pip(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setitem(sys.modules, "pip._internal.cli.main", None)
    calls = []
    monkeypatch.setattr(table_deps.subprocess, "call", lambda args: calls.append(args) or 0)

    assert table_deps._run_pip(["install", "--target", "x"]) == 0
    assert calls[0][:3] == [sys.executable, "-m", "pip"]


def test_concurrent_requests_share_one_install(monkeypatch):
    fake_find_spec({"torch": False, "transformers": False}, monkeypatch)
    release = threading.Event()
    monkeypatch.setattr(
        table_deps, "_install_via_wheels", lambda directory: release.wait(timeout=5)
    )

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


def test_wheel_compatibility_scoring():
    tags = ("cp311", "cp3", "win_amd64")
    assert table_deps._wheel_score("torch-2.5.1+cpu-cp311-cp311-win_amd64.whl", tags) > 0
    assert table_deps._wheel_score("foo-1.0-py3-none-any.whl", tags) >= 0
    assert table_deps._wheel_score("foo-1.0-cp311-cp311-linux_x86_64.whl", tags) == -1


def test_extract_wheel_handles_data_directory(tmp_path):
    wheel = tmp_path / "foo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("foo/__init__.py", "")
        archive.writestr("foo-1.0.dist-info/METADATA", "Name: foo\n")
        archive.writestr("foo-1.0.data/platlib/extra.py", "")
    target = tmp_path / "deps"
    target.mkdir()

    table_deps._extract_wheel(wheel, target)

    assert (target / "foo" / "__init__.py").exists()
    assert (target / "extra.py").exists()
    assert not (target / "foo-1.0.data").exists()
