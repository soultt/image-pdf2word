"""Tests for pdf2word pipeline."""

import json
import pytest
import numpy as np
from pathlib import Path
from pdf2word.pipeline import (
    pdf_to_docx,
    PDF2WordConverter,
    ConversionConfig,
    APILayoutPredictor,
    TableStructure,
    TableCell,
    OCRResult,
    LayoutBlock,
    PageData,
)
from pdf2word.ops import (
    coords_px_to_pt,
    coords_pt_to_emu,
    sort_table_cells,
    sort_layout_blocks,
    nms_boxes,
    merge_text_lines,
    compute_fit_font_size,
)


class TestOps:
    """Test Rust/Python operators."""

    def test_coords_px_to_pt(self):
        arr = np.array([[100, 100, 200, 200]], dtype=np.float32)
        result = coords_px_to_pt(arr, 300.0)
        expected = 100 * 72 / 300
        assert abs(result[0, 0] - expected) < 0.01

    def test_coords_pt_to_emu(self):
        arr = np.array([[72, 72, 144, 144]], dtype=np.float32)  # 1 inch = 72 pt
        result = coords_pt_to_emu(arr)
        # 1 pt = 12700 EMU, so 72 pt = 914400 EMU = 1 inch
        assert result[0, 0] == 914400

    def test_sort_table_cells(self):
        cells = np.array([
            [100, 200, 1, 0],  # row 1, col 0
            [0, 100, 0, 1],    # row 0, col 1
            [0, 0, 0, 0],      # row 0, col 0
            [100, 100, 1, 1],  # row 1, col 1
        ], dtype=np.int32)
        order = sort_table_cells(cells)
        assert order == [2, 1, 0, 3]  # row-major order

    def test_nms_boxes(self):
        boxes = np.array([
            [0, 0, 100, 100],
            [10, 10, 110, 110],  # High overlap with first
            [200, 200, 300, 300],  # Separate
        ], dtype=np.float32)
        scores = np.array([0.9, 0.8, 0.7], dtype=np.float32)
        keep = nms_boxes(boxes, scores, 0.5)
        assert 0 in keep  # Highest score
        assert 2 in keep  # No overlap
        assert 1 not in keep  # Suppressed

    def test_merge_text_lines(self):
        boxes = np.array([
            [0, 0, 100, 20],
            [0, 25, 100, 45],   # Close - same paragraph
            [0, 70, 100, 90],   # Far - new paragraph
        ], dtype=np.float32)
        heights = np.array([20, 20, 20], dtype=np.float32)
        groups = merge_text_lines(boxes, heights, 0.5)
        assert len(groups) == 2
        assert groups[0] == [0, 1]
        assert groups[1] == [2]

    def test_compute_fit_font_size(self):
        fs = compute_fit_font_size(100, 400, 200, None)
        assert 6 <= fs <= 72


class TestTableStructure:
    """Test table structure conversion."""

    def test_table_to_html_basic(self):
        converter = PDF2WordConverter()
        cells = [
            TableCell(0, 1, 0, 1, "A1", 0.9, np.zeros(4), np.zeros(4)),
            TableCell(0, 1, 1, 2, "B1", 0.9, np.zeros(4), np.zeros(4)),
            TableCell(1, 2, 0, 1, "A2", 0.9, np.zeros(4), np.zeros(4)),
            TableCell(1, 2, 1, 2, "B2", 0.9, np.zeros(4), np.zeros(4)),
        ]
        html = converter._table_to_html(2, 2, cells)
        assert "<table" in html
        assert "<tr>" in html
        assert "<td>" in html
        assert "A1" in html
        assert "B2" in html

    def test_table_to_html_with_spans(self):
        converter = PDF2WordConverter()
        cells = [
            TableCell(0, 1, 0, 2, "Header", 0.9, np.zeros(4), np.zeros(4)),  # colspan=2
            TableCell(1, 2, 0, 1, "A", 0.9, np.zeros(4), np.zeros(4)),
            TableCell(1, 2, 1, 2, "B", 0.9, np.zeros(4), np.zeros(4)),
        ]
        html = converter._table_to_html(2, 2, cells)
        assert 'colspan="2"' in html


class TestConversionConfig:
    """Test configuration."""

    def test_default_config(self):
        config = ConversionConfig()
        assert config.dpi == 300
        assert config.ocr_lang == "ch_sim+en"
        assert config.table_recognition is True
        assert config.extract_figures is True
        assert config.use_layout_api is False
        assert config.mcp_enabled is True
        assert config.mcp_port == 8765

    def test_figure_extraction_toggle(self):
        config = ConversionConfig(extract_figures=False)
        assert config.extract_figures is False

    def test_custom_config(self):
        config = ConversionConfig(dpi=400, ocr_lang="en", table_recognition=False)
        assert config.dpi == 400
        assert config.ocr_lang == "en"
        assert config.table_recognition is False


class TestAPILayoutPredictor:
    """Tests for the external layout API adapter."""

    def test_client_does_not_put_key_in_base_url(self):
        predictor = APILayoutPredictor(
            "https://generativelanguage.googleapis.com/v1beta/openai/",
            "TEST_KEY",
            "gemini-2.0-flash",
        )
        client = predictor.client
        assert "key=" not in str(client.base_url)
        assert "TEST_KEY" not in str(client.base_url)
        assert client.api_key == "TEST_KEY"

    def test_extract_json_array_skips_prose_and_handles_nesting(self):
        content = (
            "Here is the layout:\n```json\n"
            '[{"label": "Text", "bbox": [1, 2, 3, 4]}, '
            '{"label": "Table", "bbox": [5, 6, 7, 8]}]\n'
            "```\nAnd that is all."
        )
        extracted = APILayoutPredictor._extract_json_array(content)
        assert extracted is not None
        assert len(json.loads(extracted)) == 2

    def test_extract_json_array_skips_bracket_in_prose(self):
        content = 'I found [1] layout blocks. Result: [{"label": "Text", "bbox": [0, 0, 9, 9]}]'
        extracted = APILayoutPredictor._extract_json_array(content)
        assert extracted is not None
        assert json.loads(extracted)[0]["label"] == "Text"

    def test_extract_json_array_returns_none_without_array(self):
        assert APILayoutPredictor._extract_json_array("no array here") is None


class TestCLI:
    """Tests for CLI option wiring."""

    def test_convert_and_batch_expose_expected_options(self):
        from click.testing import CliRunner
        from pdf2word.cli import cli

        runner = CliRunner()
        for command in ("convert", "batch"):
            result = runner.invoke(cli, [command, "--help"])
            assert result.exit_code == 0
            assert "--workers" in result.output
            assert "--no-figures" in result.output
            assert "--lang" in result.output


class TestLayoutMode:
    """The layout model is controlled by use_layout_api, not by the base URL."""

    def test_enabled_api_without_settings_fails_before_opening_pdf(self):
        config = ConversionConfig(
            use_layout_api=True, layout_api_base="https://example.com/v1"
        )
        converter = PDF2WordConverter(config)
        with pytest.raises(ValueError, match="use_layout_api"):
            converter.convert("does-not-exist.pdf", "out.docx")

    def test_layout_predictor_is_api_when_base_and_model_set(self, monkeypatch):
        import pdf2word.pipeline as pipeline

        created = {}

        class FakeAPI:
            def __init__(self, api_base, api_key, model):
                created["args"] = (api_base, api_key, model)

        monkeypatch.setattr(pipeline, "APILayoutPredictor", FakeAPI)
        config = ConversionConfig(
            use_layout_api=True,
            layout_api_base="https://example.com/v1",
            layout_api_model="test-model",
            layout_api_key="KEY",
        )
        converter = PDF2WordConverter(config)
        assert isinstance(converter.layout_predictor, FakeAPI)
        assert created["args"] == ("https://example.com/v1", "KEY", "test-model")

    def test_disabled_api_skips_layout_model_even_with_baseurl(self, monkeypatch):
        import fitz

        config = ConversionConfig(
            layout_api_base="https://example.com/v1", max_workers=1
        )
        converter = PDF2WordConverter(config)

        def fail(*args, **kwargs):
            raise AssertionError("layout model must not be used while disabled")

        monkeypatch.setattr(converter, "_layout_predictor", fail)
        monkeypatch.setattr(converter, "_ocr", lambda crop: (
            [(np.array([[0, 0], [10, 0], [10, 10], [0, 10]], dtype=np.float32), "hello", 0.9)],
            0.01,
        ))

        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 50), "Hello")
        try:
            data = converter._process_page(page, 0, fitz.Matrix(1, 1))
        finally:
            doc.close()

        assert len(data.blocks) == 1
        block = data.blocks[0]
        assert block.label == "text"
        assert len(block.ocr_results) == 1
        assert block.ocr_results[0].text == "hello"
        assert not getattr(block, "_is_header", False)
        assert not getattr(block, "_is_footer", False)

    def test_baseurl_uses_layout_predictor(self, monkeypatch):
        import fitz

        config = ConversionConfig(
            use_layout_api=True,
            layout_api_base="https://example.com/v1",
            layout_api_model="test-model",
            max_workers=1,
        )
        converter = PDF2WordConverter(config)

        class Box:
            bbox = [10, 10, 100, 100]
            label = "Title"
            confidence = 0.9

        class Result:
            bboxes = [Box()]

        called = {"count": 0}

        def fake_predict(images):
            called["count"] += 1
            return [Result()]

        monkeypatch.setattr(converter, "_layout_predictor", fake_predict)
        monkeypatch.setattr(converter, "_ocr", lambda crop: ([], 0.0))

        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        try:
            data = converter._process_page(page, 0, fitz.Matrix(1, 1))
        finally:
            doc.close()

        assert called["count"] == 1
        assert len(data.blocks) == 1
        assert data.blocks[0].label == "title"


# Integration tests (require test PDF)
class TestIntegration:
    """Integration tests - require test fixtures."""

    @pytest.fixture
    def sample_pdf(self, tmp_path):
        """Create a simple test PDF with text, table, and image."""
        import fitz
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)  # A4
        
        # Add text
        page.insert_text((50, 50), "Test Document", fontsize=24)
        page.insert_text((50, 100), "This is a paragraph with some text content.", fontsize=12)
        
        # Add a simple table as text (for layout detection)
        page.insert_text((50, 150), "Table:", fontsize=14)
        page.insert_text((50, 180), "Col1\tCol2\tCol3", fontsize=10)
        page.insert_text((50, 200), "A\tB\tC", fontsize=10)
        page.insert_text((50, 220), "D\tE\tF", fontsize=10)
        
        # Add a rectangle as figure placeholder
        page.draw_rect(fitz.Rect(50, 260, 200, 360), color=(0, 0, 1), fill=(0.8, 0.8, 1))
        page.insert_text((60, 270), "[Figure]", fontsize=10)
        
        pdf_path = tmp_path / "test.pdf"
        doc.save(pdf_path)
        doc.close()
        return pdf_path

    def test_convert_basic(self, sample_pdf, tmp_path):
        """Test basic conversion."""
        output = tmp_path / "output.docx"
        config = ConversionConfig(table_recognition=False)  # Disable for speed
        converter = PDF2WordConverter(config)
        result = converter.convert(sample_pdf, output)
        assert result.exists()
        assert result.stat().st_size > 0

    def test_convert_with_table(self, sample_pdf, tmp_path):
        """Test conversion with table recognition."""
        output = tmp_path / "output_table.docx"
        config = ConversionConfig(table_recognition=True)
        converter = PDF2WordConverter(config)
        result = converter.convert(sample_pdf, output)
        assert result.exists()

    def test_pdf_to_docx_function(self, sample_pdf, tmp_path):
        """Test convenience function."""
        output = tmp_path / "func.docx"
        result = pdf_to_docx(sample_pdf, output)
        assert result == output
        assert output.exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])