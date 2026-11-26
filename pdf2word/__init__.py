"""pdf2word: Convert image-based PDF to editable Word documents."""

from .pipeline import pdf_to_docx, PDF2WordConverter
from .ops import (
    coords_px_to_pt,
    coords_pt_to_emu,
    sort_table_cells,
    sort_layout_blocks,
    nms_boxes,
    merge_text_lines,
    compute_fit_font_size,
)

__version__ = "0.1.0"
__all__ = [
    "pdf_to_docx",
    "PDF2WordConverter",
    "coords_px_to_pt",
    "coords_pt_to_emu",
    "sort_table_cells",
    "sort_layout_blocks",
    "nms_boxes",
    "merge_text_lines",
    "compute_fit_font_size",
]