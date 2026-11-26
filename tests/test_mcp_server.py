"""Tests for the MCP conversion implementation."""

import inspect
from pathlib import Path

import pytest

import pdf2word.mcp_server as server
from pdf2word.config_store import save_config
from pdf2word.pipeline import ConversionConfig


class FakeConverter:
    instances = []

    def __init__(self, config):
        self.config = config
        FakeConverter.instances.append(self)

    def convert(self, source, target):
        Path(target).write_bytes(b"fake docx")


@pytest.fixture(autouse=True)
def _reset_converter():
    FakeConverter.instances.clear()


def test_overrides_apply_but_api_config_is_preserved(monkeypatch, tmp_path):
    monkeypatch.setenv("PDF2WORD_APP_DIR", str(tmp_path))
    save_config(
        ConversionConfig(
            dpi=300,
            use_layout_api=True,
            layout_api_base="https://api.example.com/v1",
            layout_api_key="SECRET",
            layout_api_model="test-model",
        ),
        tmp_path / "config.json",
    )
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(b"%PDF")
    monkeypatch.setattr(server, "PDF2WordConverter", FakeConverter)

    result = server.convert_pdf_impl(
        str(pdf),
        str(tmp_path / "out.docx"),
        dpi=200,
        lang="en",
        conf_threshold=0.7,
        max_workers=2,
    )

    assert result["ok"] is True
    assert result["output_path"] == str(tmp_path / "out.docx")
    config = FakeConverter.instances[-1].config
    assert config.dpi == 200
    assert config.ocr_lang == "en"
    assert config.ocr_conf_threshold == 0.7
    assert config.max_workers == 2
    assert config.use_layout_api is True
    assert config.layout_api_base == "https://api.example.com/v1"
    assert config.layout_api_key == "SECRET"
    assert config.layout_api_model == "test-model"


def test_default_output_path(monkeypatch, tmp_path):
    monkeypatch.setenv("PDF2WORD_APP_DIR", str(tmp_path))
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(b"%PDF")
    monkeypatch.setattr(server, "PDF2WordConverter", FakeConverter)

    result = server.convert_pdf_impl(str(pdf))

    assert result["ok"] is True
    assert result["output_path"] == str(pdf.with_suffix(".docx"))


def test_invalid_inputs_return_structured_errors(tmp_path):
    missing = server.convert_pdf_impl(str(tmp_path / "missing.pdf"))
    assert missing["ok"] is False
    assert "not found" in missing["error"]

    wrong_type = tmp_path / "in.txt"
    wrong_type.write_text("x", encoding="utf-8")
    result = server.convert_pdf_impl(str(wrong_type))
    assert result["ok"] is False
    assert ".pdf" in result["error"]


def test_conversion_exception_is_captured(monkeypatch, tmp_path):
    class Exploding:
        def __init__(self, config):
            pass

        def convert(self, source, target):
            raise RuntimeError("boom")

    monkeypatch.setenv("PDF2WORD_APP_DIR", str(tmp_path))
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(b"%PDF")
    monkeypatch.setattr(server, "PDF2WordConverter", Exploding)

    result = server.convert_pdf_impl(str(pdf))

    assert result["ok"] is False
    assert result["error"] == "boom"


def test_tool_does_not_expose_api_configuration():
    parameters = inspect.signature(server.convert_pdf).parameters
    assert set(parameters) == {
        "pdf_path",
        "output_path",
        "dpi",
        "lang",
        "conf_threshold",
        "max_workers",
    }
