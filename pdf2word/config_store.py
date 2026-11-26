"""Persist ConversionConfig to a JSON file next to the application."""

from __future__ import annotations

import json
from dataclasses import asdict, fields
from pathlib import Path
from typing import Dict, Optional, Union

from .app_paths import get_config_path
from .pipeline import ConversionConfig

PathLike = Union[str, Path]

_INT_FIELDS = (
    "dpi",
    "max_workers",
    "intra_threads",
    "inter_threads",
    "figure_min_size",
    "mcp_port",
)
_FLOAT_FIELDS = ("ocr_conf_threshold", "para_v_threshold_ratio", "nms_iou_threshold")
_BOOL_FIELDS = (
    "table_recognition",
    "preserve_fonts",
    "preserve_colors",
    "merge_paragraphs",
    "extract_figures",
    "use_layout_api",
    "mcp_enabled",
)


def config_to_dict(config: ConversionConfig) -> Dict:
    """Serialize a config into a JSON-compatible dict."""
    return asdict(config)


def dict_to_config(data: Dict) -> ConversionConfig:
    """Build a config from stored data, ignoring unknown keys."""
    if not isinstance(data, dict):
        raise ValueError("Config must be a JSON object")
    valid = {f.name for f in fields(ConversionConfig)}
    clean = {key: value for key, value in data.items() if key in valid}

    for key in _INT_FIELDS:
        if key in clean:
            clean[key] = int(clean[key])
    for key in _FLOAT_FIELDS:
        if key in clean:
            clean[key] = float(clean[key])
    for key in _BOOL_FIELDS:
        if key in clean:
            value = clean[key]
            if isinstance(value, str):
                clean[key] = value.strip().lower() in ("1", "true", "yes", "on")
            else:
                clean[key] = bool(value)
    if "ocr_lang" in clean:
        clean["ocr_lang"] = str(clean["ocr_lang"])

    return ConversionConfig(**clean)


def load_config(path: Optional[PathLike] = None) -> ConversionConfig:
    """Load saved config; fall back to defaults on any read/parse error."""
    target = Path(path) if path is not None else get_config_path()
    if not target.exists():
        return ConversionConfig()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return dict_to_config(data)
    except (OSError, ValueError, TypeError):
        return ConversionConfig()


def save_config(config: ConversionConfig, path: Optional[PathLike] = None) -> Path:
    """Write config to disk and return the file path."""
    target = Path(path) if path is not None else get_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(config_to_dict(config), ensure_ascii=False, indent=2)
    target.write_text(payload, encoding="utf-8")
    return target


def reset_config(path: Optional[PathLike] = None) -> ConversionConfig:
    """Persist and return a default config."""
    config = ConversionConfig()
    save_config(config, path)
    return config
