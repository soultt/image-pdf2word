"""Tests for GUI configuration persistence."""

import json
from dataclasses import fields

from pdf2word.config_store import (
    dict_to_config,
    load_config,
    reset_config,
    save_config,
)
from pdf2word.gui import PARAM_GROUPS
from pdf2word.pipeline import ConversionConfig


def test_round_trip(tmp_path):
    config = ConversionConfig(
        dpi=200,
        ocr_lang="en",
        max_workers=2,
        extract_figures=False,
        layout_api_base="https://example.com/v1",
        layout_api_model="m",
    )
    path = tmp_path / "config.json"
    save_config(config, path)

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["dpi"] == 200
    assert load_config(path) == config


def test_missing_and_corrupt_fall_back_to_defaults(tmp_path):
    assert load_config(tmp_path / "missing.json") == ConversionConfig()
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert load_config(broken) == ConversionConfig()


def test_unknown_keys_are_ignored_and_types_coerced(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"dpi": "250", "table_recognition": "false", "unknown": "x"}),
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.dpi == 250
    assert config.table_recognition is False


def test_reset_config_persists_defaults(tmp_path):
    path = tmp_path / "config.json"
    save_config(ConversionConfig(dpi=400), path)
    assert reset_config(path) == ConversionConfig()
    assert json.loads(path.read_text(encoding="utf-8"))["dpi"] == 300


def test_gui_parameter_map_covers_all_config_fields():
    keys = {key for _, group in PARAM_GROUPS for key, _, _ in group}
    assert keys == {field.name for field in fields(ConversionConfig)}
