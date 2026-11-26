"""MCP tools exposing PDF conversion.

The bundled Windows app serves these tools over HTTP; API configuration is read
from config.json (the same file the GUI writes). There are deliberately no MCP
tools for reading or changing configuration.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Optional

from .config_store import config_to_dict, dict_to_config, load_config
from .pipeline import PDF2WordConverter

try:
    from mcp.server.fastmcp import FastMCP as _Server  # mcp 1.x
except ImportError:  # mcp 2.x renamed the class
    from mcp.server.mcpserver import MCPServer as _Server

mcp = _Server("pdf2word")

# Only these config fields can be overridden through MCP. Layout API fields
# are intentionally absent: they are managed outside MCP.
_OVERRIDABLE_FIELDS = {
    "dpi": "dpi",
    "lang": "ocr_lang",
    "conf_threshold": "ocr_conf_threshold",
    "max_workers": "max_workers",
}


def _result(ok: bool, output_path: Optional[str], error: Optional[str], started: float) -> Dict[str, Any]:
    return {
        "ok": ok,
        "output_path": output_path,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "error": error,
    }


def convert_pdf_impl(
    pdf_path: str,
    output_path: Optional[str] = None,
    dpi: Optional[int] = None,
    lang: Optional[str] = None,
    conf_threshold: Optional[float] = None,
    max_workers: Optional[int] = None,
) -> Dict[str, Any]:
    """Convert PDF to DOCX using saved config plus optional overrides."""
    started = time.perf_counter()
    try:
        source = Path(pdf_path).expanduser().resolve()
        if not source.is_file():
            return _result(False, None, f"PDF file not found: {pdf_path}", started)
        if source.suffix.lower() != ".pdf":
            return _result(False, None, f"Input must be a .pdf file: {pdf_path}", started)

        target = (
            Path(output_path).expanduser().resolve()
            if output_path
            else source.with_suffix(".docx")
        )
        if target.suffix.lower() != ".docx":
            return _result(False, None, f"Output must be a .docx path: {output_path}", started)
        target.parent.mkdir(parents=True, exist_ok=True)

        data = config_to_dict(load_config())
        supplied = {
            "dpi": dpi,
            "lang": lang,
            "conf_threshold": conf_threshold,
            "max_workers": max_workers,
        }
        for name, value in supplied.items():
            if value is not None:
                data[_OVERRIDABLE_FIELDS[name]] = value
        config = dict_to_config(data)

        converter = PDF2WordConverter(config)
        converter.convert(source, target)
        return _result(True, str(target), None, started)
    except Exception as exc:
        return _result(False, None, str(exc), started)


@mcp.tool()
def convert_pdf(
    pdf_path: str,
    output_path: Optional[str] = None,
    dpi: Optional[int] = None,
    lang: Optional[str] = None,
    conf_threshold: Optional[float] = None,
    max_workers: Optional[int] = None,
) -> Dict[str, Any]:
    """Convert a PDF file to an editable Word document.

    Uses API settings from config.json. Only dpi, lang, conf_threshold and
    max_workers can be overridden per call; API credentials are not exposed.
    """
    return convert_pdf_impl(
        pdf_path=pdf_path,
        output_path=output_path,
        dpi=dpi,
        lang=lang,
        conf_threshold=conf_threshold,
        max_workers=max_workers,
    )


