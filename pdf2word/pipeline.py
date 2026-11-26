"""Main PDF to Word conversion pipeline."""

from __future__ import annotations

import os
import tempfile
import logging
import io
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from contextlib import contextmanager

import fitz  # pymupdf
import numpy as np
from tqdm import tqdm
from docx import Document
from docx.shared import Pt, Emu, Cm, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from PIL import Image

from .ops import (
    coords_px_to_pt,
    coords_pt_to_emu,
    sort_table_cells,
    sort_layout_blocks,
    nms_boxes,
    merge_text_lines,
    compute_fit_font_size,
)
from . import table_deps

logger = logging.getLogger(__name__)

# torch.set_num_interop_threads() may only be called once per process, and only
# before any inter-op work has started. Guard it so multiple converter instances
# in the same process (batch mode, MCP server) don't raise on the second call.
_TORCH_INTEROP_CONFIGURED = False


@dataclass
class OCRResult:
    """Result of OCR on a single image crop."""
    text: str
    confidence: float
    bbox_px: np.ndarray  # [x1, y1, x2, y2] in pixels
    bbox_pt: np.ndarray  # in points


@dataclass
class TableCell:
    """A single table cell with content and span info."""
    row_start: int
    row_end: int
    col_start: int
    col_end: int
    text: str
    confidence: float
    bbox_px: np.ndarray
    bbox_pt: np.ndarray


@dataclass
class TableStructure:
    """Recognized table structure."""
    rows: int
    cols: int
    cells: List[TableCell]
    html: str
    row_lines: List[float] = field(default_factory=list)  # y-coordinates of row boundaries (in table crop coords)
    col_lines: List[float] = field(default_factory=list)  # x-coordinates of column boundaries (in table crop coords)


@dataclass
class LayoutBlock:
    """A detected layout block on a page."""
    bbox_px: np.ndarray
    bbox_pt: np.ndarray
    label: str  # 'text', 'title', 'table', 'figure', 'caption', 'header', 'footer'
    confidence: float
    reading_order: int = -1
    ocr_results: List[OCRResult] = field(default_factory=list)
    table_structure: Optional[TableStructure] = None
    figure_image: Optional[bytes] = None
    figure_mime: str = "image/png"
    caption_text: str = ""  # Associated caption text for figures/tables


@dataclass
class PageData:
    """All data for a single page."""
    page_num: int
    width_px: int
    height_px: int
    width_pt: float
    height_pt: float
    blocks: List[LayoutBlock] = field(default_factory=list)


# Minimal stand-ins for surya's layout schema. Only these fields are consumed
# by the pipeline; defining them locally keeps the layout API mode from pulling
# surya (and its transformers/torch dependency chain) into the packaged app.
@dataclass
class LayoutBox:
    polygon: List[List[float]]
    confidence: float
    label: str
    raw_label: str
    position: int
    count: int
    bbox: List[float]


@dataclass
class LayoutResult:
    bboxes: List[LayoutBox]
    image_bbox: List[float]
    raw: str
    error: bool


class APILayoutPredictor:
    """Layout predictor using external OpenAI-compatible API.
    
    Supports:
    - OpenRouter (https://openrouter.ai/api/v1) - requires key, has free quota for some models
    - Google AI Studio (https://generativelanguage.googleapis.com/v1beta/openai/) - free tier with key
    - OpenAI (https://api.openai.com/v1) - paid
    - Any OpenAI-compatible endpoint
    """
    
    def __init__(self, api_base: str, api_key: str, model: str):
        self.api_base = api_base.rstrip('/')
        self.api_key = api_key
        self.model = model
        self._client = None
    
    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI
            # Never embed the key in base_url. The OpenAI client appends the
            # endpoint path to base_url.raw_path, and httpx includes any query
            # string in raw_path -- so a "?key=..." base_url yields a corrupted
            # URL where the key absorbs the path and every request fails.
            # Google's OpenAI-compatible endpoint accepts Authorization: Bearer.
            self._client = OpenAI(
                base_url=self.api_base,
                api_key=self.api_key or "unused",
            )
        return self._client
    
    def _image_to_base64(self, image: Image.Image) -> str:
        import base64
        import io
        buffered = io.BytesIO()
        image.save(buffered, format="PNG")
        return base64.b64encode(buffered.getvalue()).decode('utf-8')

    @staticmethod
    def _extract_json_array(text: str) -> Optional[str]:
        """Return the first valid JSON array substring, or None.

        Tolerates markdown fences and surrounding reasoning text. A greedy
        regex can span several arrays, while a non-greedy one stops inside
        nested bbox arrays, so track depth/string state and skip candidates
        that are not parseable JSON arrays (e.g. a bracket in prose).
        """
        import json as _json

        pos = 0
        while pos < len(text):
            start = text.find('[', pos)
            if start < 0:
                return None
            depth = 0
            in_string = False
            escaped = False
            end = None
            for i in range(start, len(text)):
                ch = text[i]
                if in_string:
                    if escaped:
                        escaped = False
                    elif ch == '\\':
                        escaped = True
                    elif ch == '"':
                        in_string = False
                    continue
                if ch == '"':
                    in_string = True
                elif ch == '[':
                    depth += 1
                elif ch == ']':
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            if end is None:
                # An unmatched bracket in reasoning/prose must not abort the
                # search; keep looking after it for the real JSON array.
                pos = start + 1
                continue
            candidate = text[start:end + 1]
            try:
                parsed = _json.loads(candidate)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, list) and all(isinstance(item, dict) for item in parsed):
                return candidate
            # Some models wrap the array in an object: {"blocks": [...]}
            if isinstance(parsed, dict):
                for value in parsed.values():
                    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                        return _json.dumps(value)
            pos = start + 1

        # Last resort: the model hit max_tokens and left the array unterminated.
        # Keep the complete objects and close the array ourselves.
        start = text.find('[')
        last_brace = text.rfind('}')
        if start >= 0 and last_brace > start:
            candidate = text[start:last_brace + 1] + ']'
            try:
                parsed = _json.loads(candidate)
                if isinstance(parsed, list) and all(isinstance(item, dict) for item in parsed):
                    return candidate
            except (ValueError, TypeError):
                pass
        return None

    @staticmethod
    def _coerce_bbox(bbox: Any, width: int, height: int) -> Optional[List[float]]:
        """Normalise a model-provided bbox to pixel coordinates.

        Accepts a 4-list or a dict with x1/y1/x2/y2, numeric or string values,
        and both absolute pixels and normalised 0-1 fractions. Returns None
        when the box is unusable.
        """
        if isinstance(bbox, dict):
            bbox = [bbox.get(key) for key in ("x1", "y1", "x2", "y2")]
        if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
            return None
        try:
            values = [float(v) for v in bbox[:4]]
        except (TypeError, ValueError):
            return None
        if not all(v == v for v in values):  # NaN
            return None
        # Only treat values as normalised when they are unmistakably 0-1
        # fractions. Pixel boxes with small coordinates (e.g. [10, 20, 30, 40])
        # must not be rescaled just because their numbers happen to be <=1000.
        if all(0.0 <= v <= 1.0 for v in values) and any(v < 1.0 for v in values):
            values = [values[0] * width, values[1] * height,
                      values[2] * width, values[3] * height]
        x1, y1, x2, y2 = values
        x1, x2 = sorted((x1, x2))
        y1, y2 = sorted((y1, y2))
        x1 = max(0.0, min(float(width), x1))
        x2 = max(0.0, min(float(width), x2))
        y1 = max(0.0, min(float(height), y1))
        y2 = max(0.0, min(float(height), y2))
        if x2 - x1 < 1.0 or y2 - y1 < 1.0:
            return None
        return [x1, y1, x2, y2]

    def __call__(self, images: List[Image.Image]) -> List:
        """Process images and return layout results compatible with Surya."""
        import json as _json
        import time as _time

        results = []
        for img in images:
            w, h = img.size
            # Send a bounded-size copy: large vision models internally resize
            # anyway, and telling them the exact size of the image we send lets
            # us scale their boxes back to full resolution reliably.
            sent = img.copy()
            sent.thumbnail((1536, 1536), Image.LANCZOS)
            sw, sh = sent.size
            img_b64 = self._image_to_base64(sent)

            # Build prompt for layout detection
            prompt = (
                "Analyze this document image and return a JSON array of layout blocks.\n"
                f"The image is {sw} x {sh} pixels. The 'bbox' field MUST be "
                f"[x1, y1, x2, y2] in these exact pixel coordinates, spanning "
                f"the full {sw}x{sh} image.\n"
                "Each block must have: label (Text, Title, Table, Figure, Caption, "
                "Header, Footer or Section-Header), bbox, confidence (0-1) and reading_order.\n"
                "Return ONLY the JSON array, no prose, no markdown fences."
            )

            try:
                content = None
                last_error = None
                # Retry a few times for transient empty responses / rate limits
                for attempt in range(3):
                    try:
                        response = self.client.chat.completions.create(
                            model=self.model,
                            messages=[{
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": prompt},
                                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}}
                                ]
                            }],
                            max_tokens=4096,
                            temperature=0.1,
                        )
                        msg = response.choices[0].message
                        content = msg.content
                        # Reasoning models (e.g. Nemotron) may put output in
                        # reasoning / reasoning_content, or leave content empty
                        # when the token limit is consumed by reasoning.
                        if not content:
                            content = getattr(msg, 'reasoning_content', None)
                        if not content:
                            content = getattr(msg, 'reasoning', None)
                        if content and content.strip():
                            break
                        last_error = "Empty response from API"
                    except Exception as e:
                        last_error = e
                        err_str = str(e).lower()
                        if '401' in err_str or 'auth' in err_str or 'key' in err_str:
                            raise
                        if 'rate' in err_str or '429' in err_str:
                            _time.sleep(2 * (attempt + 1))

                if not content or not content.strip():
                    raise ValueError(str(last_error) if last_error else "Empty response from API")

                # Extract the first balanced JSON array, tolerating markdown
                # code fences and surrounding reasoning text.
                json_text = self._extract_json_array(content)
                if json_text is None:
                    raise ValueError("No JSON array found in API response")
                layout_data = _json.loads(json_text)

                # Convert to our local LayoutResult format
                boxes = []
                for i, block in enumerate(layout_data):
                    label = str(block.get('label', 'Text'))
                    try:
                        conf = float(block.get('confidence', 0.9))
                    except (TypeError, ValueError):
                        conf = 0.9
                    raw_bbox = next(
                        (block[key] for key in ("bbox", "box", "bbox_2d", "coordinates", "rect")
                         if key in block),
                        None,
                    )
                    bbox_px = self._coerce_bbox(raw_bbox, sw, sh)
                    if bbox_px is None:
                        logger.debug("Ignoring layout block %d with invalid bbox: %r", i, raw_bbox)
                        continue
                    # Scale boxes from the size we sent to the API back to the
                    # original full-resolution page.
                    bbox_px = [
                        max(0.0, min(float(w), bbox_px[0] * w / sw)),
                        max(0.0, min(float(h), bbox_px[1] * h / sh)),
                        max(0.0, min(float(w), bbox_px[2] * w / sw)),
                        max(0.0, min(float(h), bbox_px[3] * h / sh)),
                    ]
                    if bbox_px[2] - bbox_px[0] < 1.0 or bbox_px[3] - bbox_px[1] < 1.0:
                        continue
                    x1, y1, x2, y2 = bbox_px
                    polygon = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
                    boxes.append(LayoutBox(
                        polygon=polygon,
                        confidence=conf,
                        label=label,
                        raw_label=label,
                        position=i,
                        count=1,
                        bbox=bbox_px,
                    ))

                results.append(LayoutResult(
                    bboxes=boxes,
                    image_bbox=[0, 0, w, h],
                    raw=_json.dumps(layout_data),
                    error=False
                ))

            except Exception as e:
                # A bad response on a single page must not abort the whole PDF.
                # Report the failure in the result and let _process_page fall
                # back to full-page OCR for just that page.
                detail = f"{type(e).__name__}: {e}"
                logger.warning("Layout API failed for a page: %s", detail)
                results.append(LayoutResult(
                    bboxes=[],
                    image_bbox=[0, 0, w, h],
                    raw=detail,
                    error=True,
                ))

        return results


@dataclass
class ConversionConfig:
    """Configuration for PDF to Word conversion."""
    dpi: int = 300
    ocr_lang: str = "ch_sim+en"
    ocr_conf_threshold: float = 0.5
    layout_model: str = "surya"
    table_recognition: bool = True
    table_model: str = "microsoft/table-transformer-structure-recognition"
    preserve_fonts: bool = True
    preserve_colors: bool = True
    merge_paragraphs: bool = True
    para_v_threshold_ratio: float = 0.5
    nms_iou_threshold: float = 0.45
    max_workers: int = 4
    # CPU thread control for ML inference (RapidOCR / ONNX Runtime, PyTorch).
    # Without limits each library uses ALL cores and `max_workers` pages are
    # processed at once -> threads * cores oversubscription (CPU pegged, slower).
    # intra_threads=0 means auto: max(1, cpu_count // max_workers).
    intra_threads: int = 0
    inter_threads: int = 1
    extract_figures: bool = True  # extract and embed figures/images
    figure_min_size: int = 50  # minimum figure size in pixels
    # External API for layout detection (OpenAI-compatible)
    layout_api_base: str = ""  # e.g., "https://api.openai.com/v1" or "https://openrouter.ai/api/v1"
    layout_api_key: str = ""   # API key
    layout_api_model: str = "" # Model name, e.g., "gpt-4o-mini" or "google/gemini-2.0-flash-exp:free"
    use_layout_api: bool = False  # explicit switch; base URL alone never enables the API
    # MCP HTTP server settings (used by the GUI / packaged app)
    mcp_enabled: bool = True
    mcp_port: int = 8765


class PDF2WordConverter:
    """Main converter class."""

    def __init__(self, config: Optional[ConversionConfig] = None):
        self.config = config or ConversionConfig()
        self._configure_native_threads()
        self._init_models()

    def _init_models(self):
        """Initialize OCR and layout models lazily."""
        self._ocr = None
        self._layout_predictor = None
        self._table_structure_model = None
        self._table_structure_processor = None

    def _refresh_table_deps(self) -> None:
        """Clear the 'unavailable' cache once runtime deps finish installing."""
        if not self.config.table_recognition or not table_deps.available():
            return
        if self._table_structure_model is False:
            self._table_structure_model = None
        if self._table_structure_processor is False:
            self._table_structure_processor = None

    def _effective_intra_threads(self) -> int:
        """Intra-op threads per inference library, clamped to available cores."""
        cpu_count = os.cpu_count() or 4
        requested = int(self.config.intra_threads or 0)
        if requested > 0:
            return max(1, min(requested, cpu_count))
        workers = max(1, int(self.config.max_workers or 1))
        return max(1, min(cpu_count, cpu_count // workers))

    def _effective_inter_threads(self) -> int:
        """Inter-op threads per inference library."""
        return max(1, min(4, int(self.config.inter_threads or 1)))

    def _configure_native_threads(self):
        """Cap thread counts used by native ML libraries before they are imported.

        Both ONNX Runtime and PyTorch otherwise default to one thread per CPU
        core. With `max_workers` pages in flight that becomes
        `max_workers * cpu_count` compute threads fighting over `cpu_count`
        cores. Setting the environment variables here affects libraries that
        are imported later (torch is loaded lazily by the table model).
        """
        intra = self._effective_intra_threads()
        inter = self._effective_inter_threads()
        # OMP/MKL are read by PyTorch (and OpenMP-backed ONNX Runtime builds)
        # at import time. Our explicit config takes precedence over ambient env.
        os.environ["OMP_NUM_THREADS"] = str(intra)
        os.environ["MKL_NUM_THREADS"] = str(intra)
        logger.info(
            "Inference threads: intra=%d, inter=%d, page workers=%d",
            intra, inter, max(1, int(self.config.max_workers or 1)),
        )

    def _warn(self, message: str) -> None:
        """Report a recoverable problem to the caller if it supplied a callback."""
        callback = getattr(self, "_warning_callback", None)
        if callback is None:
            return
        try:
            callback(message)
        except Exception:
            pass

    def _configure_torch_threads(self):
        """Apply thread limits to PyTorch before any table inference runs."""
        global _TORCH_INTEROP_CONFIGURED
        try:
            import torch

            torch.set_num_threads(self._effective_intra_threads())
            if not _TORCH_INTEROP_CONFIGURED:
                try:
                    torch.set_num_interop_threads(self._effective_inter_threads())
                except RuntimeError:
                    # Already initialised elsewhere in the process; keep the
                    # existing inter-op pool instead of failing the conversion.
                    pass
                _TORCH_INTEROP_CONFIGURED = True
        except Exception as exc:  # torch is optional; table recognition degrades gracefully
            logger.debug("Could not configure PyTorch threads: %s", exc)

    @property
    def ocr(self):
        if self._ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            # Accept both "+" (documented format) and "," (older docs) as
            # separators, and drop empty entries.
            langs = [l for l in self.config.ocr_lang.replace(',', '+').split('+') if l]
            intra = self._effective_intra_threads()
            inter = self._effective_inter_threads()
            # rapidocr-onnxruntime reads these keys from Global/Det/Cls/Rec and
            # applies them to each ONNX InferenceSession. Passing the per-module
            # variants explicitly is necessary because its config updater only
            # propagates the global values in a separate code path.
            self._ocr = RapidOCR(
                langs=langs,
                intra_op_num_threads=intra,
                inter_op_num_threads=inter,
                det_intra_op_num_threads=intra,
                cls_intra_op_num_threads=intra,
                rec_intra_op_num_threads=intra,
            )
        return self._ocr

    def _validate_config(self):
        """Require complete API settings only when the API is explicitly enabled."""
        if self.config.use_layout_api and not (
            self.config.layout_api_base and self.config.layout_api_model
        ):
            raise ValueError(
                "use_layout_api is enabled but layout_api_base or layout_api_model "
                "is empty. Provide both, or disable use_layout_api to skip the "
                "layout model entirely."
            )

    @property
    def layout_predictor(self):
        if self._layout_predictor is None:
            self._validate_config()
            if not self.config.use_layout_api:
                raise RuntimeError("layout_predictor is disabled (use_layout_api=False)")
            api_key = self.config.layout_api_key or ""
            self._layout_predictor = APILayoutPredictor(
                api_base=self.config.layout_api_base,
                api_key=api_key,
                model=self.config.layout_api_model,
            )
            logger.info(
                f"Using external layout API: {self.config.layout_api_base} "
                f"with model {self.config.layout_api_model}"
            )
        return self._layout_predictor

    @property
    def table_structure_model(self):
        """Load Table Transformer for structure recognition."""
        if self._table_structure_model is None:
            if not table_deps.available():
                table_deps.request_install()
                logger.warning(
                    "表格识别依赖尚未就绪：已在后台下载，本次将表格按文字识别"
                )
                self._table_structure_model = False  # try again on next convert()
                return False
            try:
                self._configure_torch_threads()
                from transformers import TableTransformerForObjectDetection
                import torch
                self._table_structure_model = TableTransformerForObjectDetection.from_pretrained(
                    self.config.table_model
                )
                self._table_structure_model.eval()
                if torch.cuda.is_available():
                    self._table_structure_model = self._table_structure_model.cuda()
            except Exception as e:
                logger.warning(f"Failed to load table structure model: {e}")
                self._table_structure_model = False  # Mark as unavailable
        return self._table_structure_model

    @property
    def table_structure_processor(self):
        """Load Table Transformer image processor."""
        if self._table_structure_processor is None:
            if not table_deps.available():
                table_deps.request_install()
                logger.warning(
                    "表格识别依赖尚未就绪：已在后台下载，本次将表格按文字识别"
                )
                self._table_structure_processor = False
                return False
            try:
                from transformers import DetrImageProcessor
                self._table_structure_processor = DetrImageProcessor.from_pretrained(
                    self.config.table_model
                )
            except Exception as e:
                logger.warning(f"Failed to load table structure processor: {e}")
                self._table_structure_processor = False
        return self._table_structure_processor

    def convert(
        self,
        pdf_path: str | Path,
        output_path: str | Path,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        warning_callback: Optional[Callable[[str], None]] = None,
    ) -> Path:
        """Convert a PDF file to Word document.

        ``progress_callback(done, total)`` is invoked after each processed page;
        ``warning_callback(message)`` reports recoverable per-page problems
        (e.g. a layout API failure that falls back to full-page OCR).
        """
        pdf_path = Path(pdf_path)
        output_path = Path(output_path)
        self._warning_callback = warning_callback
        # Fail before processing pages: per-page error handling in the parallel
        # path would otherwise swallow this and yield empty documents.
        self._validate_config()
        self._refresh_table_deps()

        logger.info(f"Opening PDF: {pdf_path}")
        doc = fitz.open(pdf_path)
        try:
            scale = self.config.dpi / 72.0
            mat = fitz.Matrix(scale, scale)

            # Prepare page data for parallel processing
            page_args = [(page, page_num, mat) for page_num, page in enumerate(doc)]
            total_pages = len(page_args)

            # Process pages in parallel if max_workers > 1
            pages_data = []
            if self.config.max_workers > 1:
                pages_data = self._process_pages_parallel(
                    page_args,
                    progress_callback=progress_callback,
                    total_pages=total_pages,
                )
            else:
                for page, page_num, mat in tqdm(page_args, desc="Processing pages"):
                    page_data = self._process_page(page, page_num, mat)
                    pages_data.append(page_data)
                    if progress_callback is not None:
                        progress_callback(page_num + 1, total_pages)
        finally:
            doc.close()

        # Sort pages by page number (in case parallel processing changed order)
        pages_data.sort(key=lambda p: p.page_num)

        # Generate Word document
        logger.info("Generating Word document...")
        word_doc = self._generate_word(pages_data)
        word_doc.save(output_path)
        logger.info(f"Saved to: {output_path}")

        return output_path

    def _process_pages_parallel(
        self,
        page_args: List[Tuple],
        progress_callback: Optional[Callable[[int, int], None]] = None,
        total_pages: Optional[int] = None,
    ) -> List[PageData]:
        """Process multiple pages in parallel using ThreadPoolExecutor."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        total = total_pages if total_pages is not None else len(page_args)
        done_count = 0
        pages_data = []
        with ThreadPoolExecutor(max_workers=self.config.max_workers) as executor:
            # Submit all tasks
            future_to_page = {
                executor.submit(self._process_page, page, page_num, mat): page_num
                for page, page_num, mat in page_args
            }

            # Collect results with progress bar
            for future in tqdm(as_completed(future_to_page), total=len(page_args), desc="Processing pages"):
                page_num = future_to_page[future]
                try:
                    page_data = future.result()
                    pages_data.append(page_data)
                except Exception as e:
                    logger.error(f"Failed to process page {page_num}: {e}")
                    # Create empty page data to maintain order
                    pages_data.append(PageData(
                        page_num=page_num,
                        width_px=0, height_px=0,
                        width_pt=0, height_pt=0,
                        blocks=[]
                    ))
                done_count += 1
                if progress_callback is not None:
                    progress_callback(done_count, total)

        return pages_data

    def _process_page(self, page: fitz.Page, page_num: int, mat: fitz.Matrix) -> PageData:
        """Process a single page: render, layout analysis, OCR."""
        # Render page to image
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img_np = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, 3)

        width_px, height_px = pix.w, pix.h
        width_pt, height_pt = width_px * 72.0 / self.config.dpi, height_px * 72.0 / self.config.dpi

        # Layout detection is controlled by the explicit use_layout_api flag.
        # When disabled, skip the layout model entirely and OCR the whole page
        # as a single text block; base URL presence has no effect.
        use_layout_model = self.config.use_layout_api
        blocks = []
        if use_layout_model:
            # The layout predictor expects PIL images.
            img_pil = Image.fromarray(img_np)
            layout_results = self.layout_predictor([img_pil])[0]
            api_failed = bool(getattr(layout_results, "error", False))
            if api_failed:
                detail = str(getattr(layout_results, "raw", ""))[:160]
                self._warn(f"第 {page_num + 1} 页布局 API 调用失败，改为整页 OCR：{detail}")
            if not layout_results.bboxes:
                # Empty array or API failure: fall back to a single full-page
                # text block so the page still gets OCR'd instead of aborting
                # the whole conversion or producing a blank document.
                if not api_failed:
                    self._warn(f"第 {page_num + 1} 页布局 API 未返回区块，改为整页 OCR")
                layout_results.bboxes = [LayoutBox(
                    polygon=[[0, 0], [width_px, 0], [width_px, height_px], [0, height_px]],
                    confidence=1.0,
                    label="Text",
                    raw_label="Text",
                    position=0,
                    count=1,
                    bbox=[0.0, 0.0, float(width_px), float(height_px)],
                )]

            # layout_results is a LayoutResult object with a bboxes attribute containing LayoutBox objects
            for i, block in enumerate(layout_results.bboxes):
                # bbox returns [x_min, y_min, x_max, y_max] as a list
                bbox_px = np.array(block.bbox, dtype=np.float32)
                bbox_pt = coords_px_to_pt(bbox_px.reshape(1, 4), self.config.dpi)[0]

                label = block.label.lower()
                confidence = float(block.confidence) if block.confidence is not None else 1.0

                layout_block = LayoutBlock(
                    bbox_px=bbox_px,
                    bbox_pt=bbox_pt,
                    label=label,
                    confidence=confidence,
                    reading_order=i,
                )

                # OCR for text blocks
                if label in ('text', 'title', 'caption', 'header', 'footer'):
                    self._ocr_block(img_np, layout_block)
                elif label == 'table' and self.config.table_recognition:
                    self._recognize_table(img_np, layout_block)
                elif label == 'figure':
                    if not self.config.extract_figures:
                        continue
                    self._extract_figure(page, layout_block, mat)

                blocks.append(layout_block)
        else:
            logger.debug("use_layout_api is disabled; skipping layout model and OCR-ing the full page")
            bbox_px = np.array([0, 0, width_px, height_px], dtype=np.float32)
            bbox_pt = np.array([0, 0, width_pt, height_pt], dtype=np.float32)
            full_page_block = LayoutBlock(
                bbox_px=bbox_px,
                bbox_pt=bbox_pt,
                label='text',
                confidence=1.0,
                reading_order=0,
            )
            self._ocr_block(img_np, full_page_block)
            blocks.append(full_page_block)

        # Sort blocks by reading order using our own topological sort
        if blocks:
            bboxes = np.array([b.bbox_pt for b in blocks])
            block_types = np.array([{'text': 1, 'title': 0, 'table': 3, 'figure': 2, 'caption': 4, 'header': 1, 'footer': 1}.get(b.label, 1) for b in blocks], dtype=np.int32)
            sorted_idx = sort_layout_blocks(bboxes, block_types, float(width_px), float(height_px))
            blocks = [blocks[i] for i in sorted_idx]
            for new_i, b in enumerate(blocks):
                b.reading_order = new_i

        # Associate captions with figures and tables
        self._associate_captions(blocks)

        # Header/footer zone detection is only meaningful with layout info;
        # the single full-page block must never be treated as a header/footer.
        if use_layout_model:
            self._handle_headers_footers(blocks, width_pt, height_pt)

        return PageData(
            page_num=page_num,
            width_px=width_px,
            height_px=height_px,
            width_pt=width_pt,
            height_pt=height_pt,
            blocks=blocks,
        )

    def _associate_captions(self, blocks: List[LayoutBlock]):
        """Associate caption blocks with nearby figures and tables."""
        captions = [b for b in blocks if b.label == 'caption']
        targets = [b for b in blocks if b.label in ('figure', 'table')]

        for caption in captions:
            cap_center_y = (caption.bbox_pt[1] + caption.bbox_pt[3]) / 2
            cap_center_x = (caption.bbox_pt[0] + caption.bbox_pt[2]) / 2

            best_target = None
            best_dist = float('inf')

            for target in targets:
                tgt_center_y = (target.bbox_pt[1] + target.bbox_pt[3]) / 2
                tgt_center_x = (target.bbox_pt[0] + target.bbox_pt[2]) / 2

                # Prefer captions below figures, above tables
                vertical_dist = cap_center_y - tgt_center_y
                horizontal_dist = abs(cap_center_x - tgt_center_x)

                # Score: prefer close vertical distance, same column
                if target.label == 'figure':
                    # Caption usually below figure
                    if vertical_dist > 0 and vertical_dist < 100 and horizontal_dist < 100:
                        score = vertical_dist + horizontal_dist * 0.5
                        if score < best_dist:
                            best_dist = score
                            best_target = target
                elif target.label == 'table':
                    # Caption usually above table
                    if vertical_dist < 0 and vertical_dist > -100 and horizontal_dist < 100:
                        score = abs(vertical_dist) + horizontal_dist * 0.5
                        if score < best_dist:
                            best_dist = score
                            best_target = target

            if best_target:
                # Store caption text in target block
                caption_text = " ".join(r.text for r in caption.ocr_results)
                if caption_text:
                    best_target.caption_text = caption_text.strip()

    def _handle_headers_footers(self, blocks: List[LayoutBlock], page_width_pt: float, page_height_pt: float):
        """Detect and mark header/footer blocks for special handling."""
        header_zone = page_height_pt * 0.08  # Top 8%
        footer_zone = page_height_pt * 0.92  # Bottom 8%

        for block in blocks:
            if getattr(block, '_is_header', False) or getattr(block, '_is_footer', False):
                continue

            block_top = block.bbox_pt[1]
            block_bottom = block.bbox_pt[3]

            # Check if block is in header zone - add flag instead of mutating label
            if block_top < header_zone and block.label in ('text', 'title'):
                block._is_header = True
            # Check if block is in footer zone - add flag instead of mutating label
            elif block_bottom > footer_zone and block.label in ('text', 'title'):
                block._is_footer = True

    def _ocr_block(self, img: np.ndarray, block: LayoutBlock):
        """Run OCR on a layout block."""
        x1, y1, x2, y2 = map(int, block.bbox_px)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(img.shape[1], x2), min(img.shape[0], y2)

        if x2 <= x1 or y2 <= y1:
            return

        crop = img[y1:y2, x1:x2]
        if crop.size == 0:
            return

        try:
            result, elapse = self.ocr(crop)
            if result:
                for box, text, conf in result:
                    if conf < self.config.ocr_conf_threshold:
                        continue
                    # Convert box coordinates to page coordinates
                    box = np.array(box, dtype=np.float32)
                    box[:, 0] += x1
                    box[:, 1] += y1

                    # Compute bbox from quad
                    xs, ys = box[:, 0], box[:, 1]
                    ocr_bbox_px = np.array([xs.min(), ys.min(), xs.max(), ys.max()], dtype=np.float32)
                    ocr_bbox_pt = coords_px_to_pt(ocr_bbox_px.reshape(1, 4), self.config.dpi)[0]

                    block.ocr_results.append(OCRResult(
                        text=text,
                        confidence=conf,
                        bbox_px=ocr_bbox_px,
                        bbox_pt=ocr_bbox_pt,
                    ))
        except Exception as e:
            logger.warning(f"OCR failed for block {block.label}: {e}")

    def _recognize_table(self, img: np.ndarray, block: LayoutBlock):
        """Recognize table structure using Table Transformer + OCR for cells."""
        x1, y1, x2, y2 = map(int, block.bbox_px)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(img.shape[1], x2), min(img.shape[0], y2)

        if x2 <= x1 or y2 <= y1:
            return

        table_crop = img[y1:y2, x1:x2]
        if table_crop.size == 0:
            return

        # Try Table Transformer for structure recognition
        structure = self._detect_table_structure(table_crop)
        
        if structure is not None:
            # OCR each cell
            cells = self._ocr_table_cells(table_crop, structure, x1, y1)
            html = self._table_to_html(structure.rows, structure.cols, cells)
            block.table_structure = TableStructure(
                rows=structure.rows,
                cols=structure.cols,
                cells=cells,
                html=html
            )
        else:
            # Fallback: OCR whole table as text
            logger.debug("Table structure detection failed, falling back to text OCR")
            self._ocr_block(img, block)

    def _detect_table_structure(self, table_img: np.ndarray) -> Optional[TableStructure]:
        """Detect table rows/columns using Table Transformer."""
        model = self.table_structure_model
        processor = self.table_structure_processor
        
        if model is False or processor is False:
            return None
        
        if model is None or processor is None:
            return None

        try:
            import torch
            from PIL import Image as PILImage
            
            # Convert to PIL
            pil_img = PILImage.fromarray(table_img)
            
            # Prepare inputs
            inputs = processor(images=pil_img, return_tensors="pt")
            if torch.cuda.is_available():
                inputs = {k: v.cuda() for k, v in inputs.items()}
            
            # Inference
            with torch.no_grad():
                outputs = model(**inputs)
            
            # Post-process
            target_sizes = torch.tensor([pil_img.size[::-1]])
            if torch.cuda.is_available():
                target_sizes = target_sizes.cuda()
            
            results = processor.post_process_object_detection(
                outputs, threshold=0.5, target_sizes=target_sizes
            )[0]
            
            # Parse results: labels are typically 'table', 'table column', 'table row', 
            # 'table column header', 'table projected row header', 'table spanning cell'
            boxes = results["boxes"].cpu().numpy()
            labels = results["labels"].cpu().numpy()
            scores = results["scores"].cpu().numpy()
            
            # Extract row and column boxes
            row_boxes = boxes[labels == 1]  # table row
            col_boxes = boxes[labels == 2]  # table column
            
            # Also check for header rows/columns
            header_row_boxes = boxes[labels == 3]  # table column header
            header_col_boxes = boxes[labels == 4]  # table projected row header
            
            # Combine header with regular
            if len(header_row_boxes) > 0:
                row_boxes = np.vstack([row_boxes, header_row_boxes])
            if len(header_col_boxes) > 0:
                col_boxes = np.vstack([col_boxes, header_col_boxes])
            
            if len(row_boxes) == 0 or len(col_boxes) == 0:
                return None
            
            # Sort rows by y, columns by x
            row_boxes = row_boxes[row_boxes[:, 1].argsort()]  # sort by y1
            col_boxes = col_boxes[col_boxes[:, 0].argsort()]  # sort by x1
            
            # Deduplicate nearby lines (NMS-like)
            row_lines = self._deduplicate_lines(row_boxes[:, 1], axis='horizontal')
            col_lines = self._deduplicate_lines(col_boxes[:, 0], axis='vertical')
            
            rows = len(row_lines) - 1
            cols = len(col_lines) - 1
            
            if rows <= 0 or cols <= 0:
                return None
            
            return TableStructure(
                rows=rows,
                cols=cols,
                cells=[],  # Will be filled by _ocr_table_cells
                html="",
                row_lines=row_lines.tolist(),
                col_lines=col_lines.tolist()
            )
            
        except Exception as e:
            logger.warning(f"Table structure detection failed: {e}")
            return None

    def _deduplicate_lines(self, positions: np.ndarray, axis: str, threshold: float = 5.0) -> np.ndarray:
        """Deduplicate nearby line positions."""
        if len(positions) == 0:
            return np.array([])
        positions = np.sort(positions)
        keep = [positions[0]]
        for pos in positions[1:]:
            if pos - keep[-1] > threshold:
                keep.append(pos)
        return np.array(keep)

    def _ocr_table_cells(self, table_img: np.ndarray, structure: TableStructure, 
                         offset_x: int, offset_y: int) -> List[TableCell]:
        """OCR each cell in the detected table structure."""
        rows = structure.rows
        cols = structure.cols
        h, w = table_img.shape[:2]

        # Use detected row/col lines if available, otherwise fall back to uniform grid
        if structure.row_lines and structure.col_lines:
            row_lines = np.array(structure.row_lines, dtype=np.float32)
            col_lines = np.array(structure.col_lines, dtype=np.float32)
            # Ensure they span the full image
            row_lines = np.clip(row_lines, 0, h)
            col_lines = np.clip(col_lines, 0, w)
        else:
            row_lines = np.linspace(0, h, rows + 1, dtype=np.float32)
            col_lines = np.linspace(0, w, cols + 1, dtype=np.float32)

        cells = []

        for r in range(rows):
            for c in range(cols):
                y1, y2 = row_lines[r], row_lines[r + 1]
                x1, x2 = col_lines[c], col_lines[c + 1]

                # Add small padding
                pad = 2
                cy1, cy2 = max(0, int(y1 + pad)), min(h, int(y2 - pad))
                cx1, cx2 = max(0, int(x1 + pad)), min(w, int(x2 - pad))

                if cy2 <= cy1 or cx2 <= cx1:
                    cells.append(TableCell(
                        row_start=r, row_end=r + 1,
                        col_start=c, col_end=c + 1,
                        text="", confidence=0.0,
                        bbox_px=np.array([x1 + offset_x, y1 + offset_y, x2 + offset_x, y2 + offset_y], dtype=np.float32),
                        bbox_pt=np.array([0, 0, 0, 0], dtype=np.float32)
                    ))
                    continue

                cell_crop = table_img[cy1:cy2, cx1:cx2]

                # OCR the cell
                text = ""
                conf = 0.0
                try:
                    result, _ = self.ocr(cell_crop)
                    if result:
                        # Combine all text in cell
                        texts = [t for _, t, cf in result if cf >= self.config.ocr_conf_threshold]
                        text = " ".join(texts)
                        confs = [cf for _, _, cf in result if cf >= self.config.ocr_conf_threshold]
                        conf = float(np.mean(confs)) if confs else 0.0
                except Exception:
                    pass

                # Convert bbox to page coordinates (use original line coordinates without padding for bbox)
                bbox_px = np.array([
                    x1 + offset_x, y1 + offset_y,
                    x2 + offset_x, y2 + offset_y
                ], dtype=np.float32)
                bbox_pt = coords_px_to_pt(bbox_px.reshape(1, 4), self.config.dpi)[0]

                cells.append(TableCell(
                    row_start=r, row_end=r + 1,
                    col_start=c, col_end=c + 1,
                    text=text, confidence=conf,
                    bbox_px=bbox_px, bbox_pt=bbox_pt
                ))

        return cells

    def _table_to_html(self, rows: int, cols: int, cells: List[TableCell]) -> str:
        """Convert table cells to HTML."""
        # Build cell map for colspan/rowspan
        cell_map = {}
        for cell in cells:
            key = (cell.row_start, cell.col_start)
            cell_map[key] = cell
        
        html_parts = ['<table border="1" style="border-collapse: collapse; width: 100%;">']
        
        for r in range(rows):
            html_parts.append('<tr>')
            for c in range(cols):
                # Skip if this position is covered by a previous cell's span
                covered = False
                for (rr, cc), cell in cell_map.items():
                    if rr <= r < cell.row_end and cc <= c < cell.col_end:
                        if rr != r or cc != c:
                            covered = True
                            break
                if covered:
                    continue
                
                cell = cell_map.get((r, c))
                if cell is None:
                    html_parts.append('<td></td>')
                    continue
                
                # Build cell attributes
                attrs = []
                if cell.row_end - cell.row_start > 1:
                    attrs.append(f'rowspan="{cell.row_end - cell.row_start}"')
                if cell.col_end - cell.col_start > 1:
                    attrs.append(f'colspan="{cell.col_end - cell.col_start}"')
                
                attr_str = " " + " ".join(attrs) if attrs else ""
                cell_text = (cell.text
                    .replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;"))
                html_parts.append(f'<td{attr_str}>{cell_text}</td>')
            html_parts.append('</tr>')
        
        html_parts.append('</table>')
        return "\n".join(html_parts)

    def _extract_figure(self, page: fitz.Page, block: LayoutBlock, mat: fitz.Matrix):
        """Extract figure/image from page region."""
        # Check minimum size
        w_px = block.bbox_px[2] - block.bbox_px[0]
        h_px = block.bbox_px[3] - block.bbox_px[1]
        if w_px < self.config.figure_min_size or h_px < self.config.figure_min_size:
            return

        try:
            # Render just the figure region at higher quality
            # Convert bbox from pixels (at self.config.dpi) back to PDF points
            scale = self.config.dpi / 72.0
            x1_pt = block.bbox_px[0] / scale
            y1_pt = block.bbox_px[1] / scale
            x2_pt = block.bbox_px[2] / scale
            y2_pt = block.bbox_px[3] / scale
            
            clip = fitz.Rect(x1_pt, y1_pt, x2_pt, y2_pt)
            
            # Render at 2x for better quality
            fig_mat = fitz.Matrix(2 * scale, 2 * scale)
            pix = page.get_pixmap(matrix=fig_mat, clip=clip, alpha=False)
            
            if pix.width < 10 or pix.height < 10:
                return
            
            # Convert to PNG bytes
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            block.figure_image = buf.getvalue()
            block.figure_mime = "image/png"
            
        except Exception as e:
            logger.warning(f"Figure extraction failed: {e}")

    def _generate_word(self, pages_data: List[PageData]) -> Document:
        """Generate Word document from processed pages."""
        doc = Document()

        # Set default style
        style = doc.styles['Normal']
        font = style.font
        font.name = 'Calibri'
        font.size = Pt(11)
        style.paragraph_format.space_after = Pt(6)
        style.paragraph_format.line_spacing = 1.15

        # Configure East Asian font fallback
        style.element.rPr.rFonts.set(qn('w:eastAsia'), 'Microsoft YaHei')

        for page_data in pages_data:
            self._render_page(doc, page_data)

            # Add page break except for last page
            if page_data != pages_data[-1]:
                doc.add_page_break()

        return doc

    def _render_page(self, doc: Document, page: PageData):
        """Render a single page's blocks into the Word document."""
        for block in page.blocks:
            # Check header/footer via flags instead of mutated labels
            label = 'header' if getattr(block, '_is_header', False) else (
                'footer' if getattr(block, '_is_footer', False) else block.label
            )
            if label in ('text', 'title', 'caption', 'header', 'footer'):
                self._render_text_block(doc, block, page.width_pt, label)
            elif label == 'table':
                self._render_table(doc, block)
            elif label == 'figure' and self.config.extract_figures:
                self._render_figure(doc, block)

    def _render_text_block(self, doc: Document, block: LayoutBlock, page_width_pt: float = None,
                           block_type: Optional[str] = None):
        """Render text block with merged paragraphs."""
        if not block.ocr_results:
            return
        block_type = block_type or block.label

        # Sort OCR results by reading order (top-to-bottom, left-to-right)
        ocr_results = sorted(block.ocr_results, key=lambda r: (r.bbox_pt[1], r.bbox_pt[0]))

        if self.config.merge_paragraphs:
            # Group into paragraphs
            boxes = np.array([r.bbox_pt for r in ocr_results])
            heights = np.array([r.bbox_pt[3] - r.bbox_pt[1] for r in ocr_results])
            para_groups = merge_text_lines(boxes, heights, self.config.para_v_threshold_ratio)

            for group in para_groups:
                if not group:
                    continue
                texts = [ocr_results[i].text for i in group]
                combined_text = ' '.join(texts)

                # Use first result's bbox for formatting
                first = ocr_results[group[0]]
                self._add_paragraph(doc, combined_text, first.bbox_pt, block_type, page_width_pt)
        else:
            for result in ocr_results:
                self._add_paragraph(doc, result.text, result.bbox_pt, block_type, page_width_pt)

    def _add_paragraph(self, doc: Document, text: str, bbox_pt: np.ndarray, block_type: str,
                       page_width_pt: float = None):
        """Add a formatted paragraph to the document."""
        if not text.strip():
            return

        p = doc.add_paragraph()
        run = p.add_run(text)

        # Font size estimation
        height_pt = bbox_pt[3] - bbox_pt[1]
        # The Rust binding requires the ratio argument explicitly (the Python
        # fallback only has a default); passing None uses the default 0.5 in both.
        font_size = compute_fit_font_size(len(text), bbox_pt[2] - bbox_pt[0], height_pt, None)
        run.font.size = Pt(max(8, min(font_size, 72)))

        # Bold for titles
        if block_type == 'title':
            run.bold = True
            run.font.size = Pt(max(run.font.size.pt if run.font.size else 11, 14))

        # Alignment estimation - use actual page width if available
        width_pt = bbox_pt[2] - bbox_pt[0]
        page_center = (page_width_pt or 300) / 2
        block_center = (bbox_pt[0] + bbox_pt[2]) / 2

        if abs(block_center - page_center) < width_pt * 0.1:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        elif bbox_pt[0] > page_center:
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT

        # Spacing
        p.paragraph_format.space_before = Pt(2)
        p.paragraph_format.space_after = Pt(4)

    def _add_caption(self, doc: Document, caption_text: str, label: str):
        """Add a figure/table caption."""
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(f"{label}: {caption_text}")
        run.italic = True
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
        p.paragraph_format.space_before = Pt(2)
        p.paragraph_format.space_after = Pt(8)

    def _render_table(self, doc: Document, block: LayoutBlock):
        """Render table block as a proper Word table."""
        if block.table_structure and block.table_structure.cells:
            self._table_structure_to_docx(doc, block.table_structure)
        elif block.table_structure and block.table_structure.html:
            self._html_table_to_docx(doc, block.table_structure.html)
        elif block.ocr_results:
            # Fallback: render as text with tabs
            self._render_text_block(doc, block)
        else:
            p = doc.add_paragraph("[Table detected - recognition not implemented]")
            p.runs[0].italic = True

        # Add caption if available
        if block.caption_text:
            self._add_caption(doc, block.caption_text, "Table")

    def _table_structure_to_docx(self, doc: Document, table_struct: TableStructure):
        """Convert TableStructure to docx table."""
        rows = table_struct.rows
        cols = table_struct.cols
        cells = table_struct.cells

        if rows == 0 or cols == 0:
            return

        # Create cell map
        cell_map = {}
        for cell in cells:
            key = (cell.row_start, cell.col_start)
            cell_map[key] = cell

        # Add table
        table = doc.add_table(rows=rows, cols=cols, style='Table Grid')
        table.alignment = WD_TABLE_ALIGNMENT.CENTER

        # Set column widths proportionally (approximate)
        col_widths = [0] * cols
        for cell in cells:
            if cell.row_start == 0:
                width_pt = cell.bbox_pt[2] - cell.bbox_pt[0]
                for c in range(cell.col_start, cell.col_end):
                    col_widths[c] = max(col_widths[c], width_pt / (cell.col_end - cell.col_start))

        for c, width_pt in enumerate(col_widths):
            if width_pt > 0:
                table.columns[c].width = Emu(int(width_pt * 12700))

        # Track merged cells to handle rowspan/colspan correctly
        merged = [[False] * cols for _ in range(rows)]

        for r in range(rows):
            for c in range(cols):
                if merged[r][c]:
                    continue

                cell = cell_map.get((r, c))
                doc_cell = table.cell(r, c)

                if cell:
                    # Set text
                    doc_cell.text = cell.text

                    # Apply spans - merge to bottom-right cell
                    rowspan = cell.row_end - cell.row_start
                    colspan = cell.col_end - cell.col_start
                    if rowspan > 1 or colspan > 1:
                        end_r = min(r + rowspan - 1, rows - 1)
                        end_c = min(c + colspan - 1, cols - 1)
                        if end_r > r or end_c > c:
                            doc_cell.merge(table.cell(end_r, end_c))

                        # Mark all covered cells as merged
                        for rr in range(r, end_r + 1):
                            for cc in range(c, end_c + 1):
                                if rr < rows and cc < cols:
                                    merged[rr][cc] = True

                    # Format cell
                    for paragraph in doc_cell.paragraphs:
                        paragraph.paragraph_format.space_before = Pt(1)
                        paragraph.paragraph_format.space_after = Pt(1)
                        for run in paragraph.runs:
                            run.font.size = Pt(9)
                            run.font.name = 'Calibri'

        # Add spacing after table
        doc.add_paragraph()

    def _html_table_to_docx(self, doc: Document, html: str):
        """Convert HTML table to docx table using lxml."""
        try:
            from lxml import etree
            
            # Parse HTML
            parser = etree.HTMLParser()
            tree = etree.fromstring(html.encode('utf-8'), parser)
            
            # Find table element
            table_elem = tree.xpath('//table')[0]
            
            # Extract rows
            rows = table_elem.xpath('.//tr')
            if not rows:
                return
            
            # Determine max columns
            max_cols = 0
            row_cells = []
            for row in rows:
                cells = row.xpath('./td | ./th')
                row_cells.append(cells)
                # Count effective columns considering colspan
                col_count = sum(int(cell.get('colspan', 1)) for cell in cells)
                max_cols = max(max_cols, col_count)
            
            if max_cols == 0:
                return
            
            # Create docx table
            table = doc.add_table(rows=len(rows), cols=max_cols, style='Table Grid')
            table.alignment = WD_TABLE_ALIGNMENT.CENTER
            
            # Track merged cells
            merged = [[False] * max_cols for _ in range(len(rows))]
            
            for r, cells in enumerate(row_cells):
                c = 0
                for cell in cells:
                    # Find next available column
                    while c < max_cols and merged[r][c]:
                        c += 1
                    if c >= max_cols:
                        break
                    
                    rowspan = int(cell.get('rowspan', 1))
                    colspan = int(cell.get('colspan', 1))
                    
                    doc_cell = table.cell(r, c)
                    doc_cell.text = (cell.text or "").strip()
                    
                    # Apply merges
                    if rowspan > 1 or colspan > 1:
                        end_r = min(r + rowspan - 1, len(rows) - 1)
                        end_c = min(c + colspan - 1, max_cols - 1)
                        if end_r > r or end_c > c:
                            doc_cell.merge(table.cell(end_r, end_c))
                        
                        # Mark as merged
                        for rr in range(r, end_r + 1):
                            for cc in range(c, end_c + 1):
                                if rr < len(rows) and cc < max_cols:
                                    merged[rr][cc] = True
                    
                    c += colspan
            
            doc.add_paragraph()
            
        except Exception as e:
            logger.warning(f"HTML table parsing failed: {e}")
            p = doc.add_paragraph("[Table: HTML parsing failed]")
            p.runs[0].italic = True

    def _render_figure(self, doc: Document, block: LayoutBlock):
        """Render figure/image block by embedding the extracted image."""
        if block.figure_image:
            try:
                # Add image to document
                img_stream = io.BytesIO(block.figure_image)
                
                # Calculate size in inches (from points)
                width_pt = block.bbox_pt[2] - block.bbox_pt[0]
                height_pt = block.bbox_pt[3] - block.bbox_pt[1]
                width_in = width_pt / 72.0
                height_in = height_pt / 72.0
                
                # Limit max size
                max_width = 6.0  # inches
                max_height = 8.0
                if width_in > max_width:
                    ratio = max_width / width_in
                    width_in = max_width
                    height_in *= ratio
                if height_in > max_height:
                    ratio = max_height / height_in
                    height_in = max_height
                    width_in *= ratio
                
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                run = p.add_run()
                run.add_picture(img_stream, width=Inches(width_in), height=Inches(height_in))
                
                # Add caption if available
                if hasattr(block, 'caption_text') and block.caption_text:
                    self._add_caption(doc, block.caption_text, "Figure")
                
            except Exception as e:
                logger.warning(f"Figure embedding failed: {e}")
                p = doc.add_paragraph(f"[Figure: {block.bbox_pt[0]:.0f},{block.bbox_pt[1]:.0f}]")
                p.runs[0].italic = True
                p.runs[0].font.color.rgb = RGBColor(0x80, 0x80, 0x80)
        else:
            # Placeholder
            p = doc.add_paragraph(f"[Figure: {block.bbox_pt[0]:.0f},{block.bbox_pt[1]:.0f} "
                                  f"{block.bbox_pt[2]:.0f},{block.bbox_pt[3]:.0f}]")
            p.runs[0].italic = True
            p.runs[0].font.color.rgb = RGBColor(0x80, 0x80, 0x80)


def pdf_to_docx(
    pdf_path: str | Path,
    output_path: str | Path,
    config: Optional[ConversionConfig] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    warning_callback: Optional[Callable[[str], None]] = None,
) -> Path:
    """Convenience function for single-file conversion."""
    converter = PDF2WordConverter(config)
    return converter.convert(
        pdf_path,
        output_path,
        progress_callback=progress_callback,
        warning_callback=warning_callback,
    )


@contextmanager
def temp_directory():
    """Context manager for temporary directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)