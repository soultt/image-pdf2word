# pdf2word

English | [简体中文](README.zh-CN.md)

Convert image-based PDFs to editable Word documents (.docx) with OCR, layout analysis, and structure preservation.

## Features

- **OCR**: Multi-language support via RapidOCR (ONNX Runtime) - Chinese, English, and 80+ languages
- **Layout Analysis**: External layout API when configured; otherwise the layout model is skipped and the page is OCR'd as a single text block
- **Reading Order**: Intelligent topological sorting for multi-column layouts
- **Table Recognition**: Table Transformer (Microsoft) for structure detection + cell-level OCR → native docx tables with rowspan/colspan
- **Figure Extraction**: High-quality image cropping and embedding with caption association
- **Caption Association**: Automatic linking of captions to figures/tables
- **Header/Footer Detection**: Zone-based detection with special handling
- **High Performance**: Core operators (coordinate transforms, NMS, sorting) implemented in Rust via PyO3
- **Parallel Processing**: Multi-threaded page processing with configurable workers
- **Batch Processing**: Convert multiple PDFs in parallel
- **Configurable**: DPI, languages, confidence thresholds, paragraph merging
- **Docker Support**: Multi-stage builds for production deployment
- **Windows GUI**: Tkinter desktop app with parameter panel and conversion history

## Installation

### Prerequisites

- Python 3.9+
- Rust toolchain (for building core operators): `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`
- ONNX Runtime (included via `rapidocr-onnxruntime`)

### Development Install

```bash
# Clone and enter directory
cd pdf2word

# Install Python dependencies
pip install -e ".[dev]"

# Build Rust extensions (requires maturin)
maturin develop --release

# Verify installation
python -m pdf2word doctor
```

### Quick Install (without Rust)

The package includes pure-Python fallbacks for all Rust operators:

```bash
pip install -e .
python -m pdf2word doctor --no-test-rust
```

PyTorch and Transformers are **not** required for normal OCR conversion. They
are only needed for table structure recognition and are installed on demand
into the `deps/` folder on first use (see [Table recognition dependencies](#table-recognition-dependencies)).
To install them eagerly in a source environment:

```bash
pip install -e ".[table]"
```

### Docker Install

```bash
# Build image
docker build -t pdf2word .

# Run conversion
docker run --rm -v $(pwd)/input:/app/input:ro -v $(pwd)/output:/app/output \
  pdf2word convert /app/input/document.pdf /app/output/document.docx

# Or use docker-compose
docker-compose up pdf2word
```

## Usage

### Command Line

```bash
# Single file
pdf2word convert input.pdf output.docx

# With options
pdf2word convert input.pdf output.docx --dpi 300 --lang ch_sim+en --conf-threshold 0.6

# Table recognition options
pdf2word convert input.pdf output.docx --table-model microsoft/table-transformer-structure-recognition --no-table

# Figure extraction options
pdf2word convert input.pdf output.docx --no-figures --figure-min-size 50

# Parallel processing
pdf2word convert input.pdf output.docx --workers 4

# External layout API (explicit switch, requires both settings)
pdf2word convert input.pdf output.docx --use-layout-api \
  --layout-api-base https://api.openai.com/v1 --layout-api-model gpt-4o-mini

# Batch conversion
pdf2word batch ./pdfs ./output --recursive --pattern "*.pdf"

# Check environment
pdf2word doctor
```

### Windows desktop app

A Tkinter GUI is available on Windows (the CLI remains unchanged):

```bash
# Run from source
python -m pdf2word gui
# equivalent: python -m pdf2word.gui

# Build the Windows app (onedir)
build_windows.bat
# output: dist/pdf2word/pdf2word.exe (keep the whole dist/pdf2word folder together)
```

The GUI supports adding multiple PDFs, choosing an output directory, editing
all conversion parameters, progress/log display, and a history tab with search,
open-output and retry actions. It places an icon in the system tray: closing the
window hides it to the tray, and the tray menu offers "显示窗口" and "退出".
Only one instance per installation can run — launching again signals the
running instance to restore/focus its window instead of starting a second copy.

`config.json` and `history.db` are stored **next to the application**: next to
`pdf2word.exe` for packaged builds, and in the project root for source runs.
Set `PDF2WORD_APP_DIR` to override that location. The API key is stored in
plain text in `config.json`, so protect that file if a real key is saved. The
PyInstaller bundle is large because it includes ONNX Runtime and the Rust
extension; **torch / transformers are no longer bundled** — they are downloaded
into `dist/pdf2word/deps/` on first table recognition. The build uses
**onedir**: the launcher exe sits in `dist/pdf2word/` with its dependencies
beside it, so startup takes a few seconds instead of unpacking a ~2 GB onefile
archive on every launch. Zip that folder to distribute it, and keep
`config.json`, `history.db` and `deps/` next to the exe. Unused transitive
dependencies (bitsandbytes, surya, pyarrow, numba) are excluded by
`pdf2word.spec`.

Build requires PyInstaller (`build_windows.bat` installs it automatically).

### MCP server (HTTP)

The Windows executable starts an HTTP MCP server automatically when the GUI
starts (configurable in the GUI's "MCP 服务" tab). Source runs also start it
when launching the GUI. Connect with:

```json
{
  "mcpServers": {
    "pdf2word": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

The port and enable flag are stored in `config.json` next to the application;
the GUI can change them with "应用并重启 MCP". Only localhost is bound.

The only tool is `convert_pdf(pdf_path, output_path=None, dpi=None,
lang=None, conf_threshold=None, max_workers=None)`. It returns `ok`,
`output_path`, `duration_ms` and `error`.

API configuration is **not** managed through MCP: `use_layout_api` and
`layout_api_base/key/model` are read from `config.json` (written by the GUI,
CLI or edited directly). MCP exposes no tools to read or change configuration.

### Layout detection modes

Layout detection is controlled by the explicit `--use-layout-api` flag (also
available as a GUI checkbox), not by the presence of a base URL.

- **Default (`use_layout_api=False`)**: no layout model is loaded. The whole page is OCR'd as a single text block, so tables, figures and captions are not separately detected and multi-column reading order is not preserved.
- **Enabled (`--use-layout-api`)**: requires both `--layout-api-base` and `--layout-api-model`; otherwise conversion fails with a configuration error. An external OpenAI-compatible layout API detects text/title/table/figure/caption blocks.

### Table recognition dependencies

Table Transformer (PyTorch + Transformers) is optional and is **not shipped in
the packaged app**. The first time a table block is actually recognized, the
converter starts a background download into `deps/` next to `pdf2word.exe`
(project root for source runs); that conversion falls back to plain text OCR
for the table, and the next conversion uses Table Transformer. Progress and
errors appear in the GUI log panel (or in CLI logs). Details:

- CPU-only PyTorch wheel (~200 MB) from `download.pytorch.org/whl/cpu`, plus
  pinned Transformers and its dependencies from PyPI. Wheels are downloaded
  and unpacked with the standard library — **no pip and no system Python are
  required** — using a `.installing` lock and an `install.log`. Source runs
  fall back to pip only if direct wheel download fails.
- No network access, blocked download or incompatible platform merely disables
  table recognition — the rest of the conversion still succeeds.
- `PDF2WORD_APP_DIR` also moves `deps/`. Set `PDF2WORD_NO_AUTO_DEPS=1` to
  disable automatic installation and install manually instead.
- Back up or delete `deps/` together with the app; deleting it re-triggers the
  download on next use.

### Python API

```python
from pdf2word import pdf_to_docx, ConversionConfig

# Simple conversion
pdf_to_docx("scan.pdf", "output.docx")

# With custom config
config = ConversionConfig(
    dpi=300,
    ocr_lang="ch_sim+en",
    ocr_conf_threshold=0.6,
    table_recognition=True,
    table_model="microsoft/table-transformer-structure-recognition",
    merge_paragraphs=True,
    max_workers=4,
)
pdf_to_docx("scan.pdf", "output.docx", config)

# Advanced usage
from pdf2word import PDF2WordConverter
converter = PDF2WordConverter(config)
converter.convert("scan.pdf", "output.docx")
```

## Architecture

```
pdf2word/
├── pdf2word/              # Python package
│   ├── __init__.py        # Exports
│   ├── ops.py             # Rust bindings + pure-Python fallbacks
│   ├── pipeline.py        # Main conversion pipeline
│   ├── cli.py             # Click-based CLI
│   ├── gui.py             # Tkinter desktop GUI
│   ├── app_paths.py       # Config/history paths next to the app
│   ├── config_store.py    # JSON config persistence
│   ├── history.py         # SQLite conversion history
│   ├── mcp_server.py      # Shared MCP tool definitions
│   ├── mcp_http.py        # HTTP MCP server for GUI / exe
│   ├── table_deps.py      # On-demand torch/transformers installer
│   └── __main__.py        # CLI / GUI entry point
├── gui_main.py            # PyInstaller entry script
├── pdf2word.spec          # PyInstaller Windows build spec
├── build_windows.bat      # One-click Windows build
├── src/
│   └── lib.rs             # Rust core operators (PyO3)
├── tests/                 # Pytest tests
├── .github/workflows/     # GitHub Actions CI/CD
├── Dockerfile             # Multi-stage Docker build
├── docker-compose.yml     # Development/production compose
├── pyproject.toml         # Build config (maturin + pytest)
├── Cargo.toml             # Rust crate config
└── README.md
```

### Core Rust Operators (in `src/lib.rs`)

| Function | Purpose | Speedup vs Python |
|----------|---------|-------------------|
| `coords_px_to_pt` | Batch pixel→point coordinate conversion | ~20-50× |
| `coords_pt_to_emu` | Point→EMU for precise OOXML positioning | ~20-50× |
| `sort_table_cells` | Row-major table cell sorting | ~10-20× |
| `sort_layout_blocks` | Topological reading-order sort | ~10-50× |
| `nms_boxes` | Non-maximum suppression for boxes | ~5-10× |
| `merge_text_lines` | Group text lines into paragraphs | ~5-10× |
| `compute_fit_font_size` | Estimate font size to fit bbox | ~10× |

### Data Flow

```
PDF (fitz) 
  → Render pages @ DPI (pixmap)
  → Layout detection (Surya)
  → Reading order (Surya OrderPredictor)
  → For each block:
      ├─ Text/Title/Caption → OCR (RapidOCR)
      ├─ Table → Table Transformer (structure) + cell OCR
      └─ Figure → High-quality crop + caption association
  → Sort blocks by reading order
  → Associate captions with figures/tables
  → Detect headers/footers by zone
  → Generate Word (python-docx)
      ├─ Paragraphs with font size estimation
      ├─ Native docx tables with spans
      ├─ Embedded images with captions
      └─ Header/footer handling
```

## Configuration

```python
@dataclass
class ConversionConfig:
    dpi: int = 300                              # Render resolution
    ocr_lang: str = "ch_sim+en"                 # OCR languages (plus-separated, e.g. "ch_sim+en")
    ocr_conf_threshold: float = 0.5             # Min OCR confidence
    layout_model: str = "surya"                 # Layout detector
    table_recognition: bool = True              # Enable table processing
    table_model: str = "microsoft/table-transformer-structure-recognition"
    preserve_fonts: bool = True                 # Estimate font sizes
    preserve_colors: bool = True                # Keep text colors
    merge_paragraphs: bool = True               # Merge lines to paragraphs
    para_v_threshold_ratio: float = 0.5         # Paragraph merge threshold
    nms_iou_threshold: float = 0.45             # NMS IoU threshold
    max_workers: int = 4                        # Parallel page workers
    intra_threads: int = 0                      # Threads per OCR/table model; 0 = auto (CPU cores // max_workers)
    inter_threads: int = 1                      # Inter-op threads per inference library
    extract_figures: bool = True                # Extract and embed figures/images
    figure_min_size: int = 50                   # Min figure size (px)
```

## Docker Usage

### Build

```bash
# Production image (multi-stage, ~500MB)
docker build -t pdf2word .

# Development image (with source mounted)
docker-compose up -d pdf2word-dev
docker exec -it pdf2word-dev bash
```

### Run Conversion

```bash
# Single file
docker run --rm \
  -v $(pwd)/input:/app/input:ro \
  -v $(pwd)/output:/app/output \
  pdf2word convert /app/input/doc.pdf /app/output/doc.docx

# Batch
docker run --rm \
  -v $(pwd)/pdfs:/app/input:ro \
  -v $(pwd)/output:/app/output \
  pdf2word batch /app/input /app/output --recursive
```

### docker-compose.yml

```yaml
services:
  pdf2word:
    build: .
    volumes:
      - ./input:/app/input:ro
      - ./output:/app/output
  pdf2word-dev:
    build:
      target: python-builder
    volumes:
      - .:/app
    command: sleep infinity
```

## Performance Tips

1. **Use GPU**: Install `onnxruntime-gpu` instead of `onnxruntime` for NVIDIA GPUs
2. **Adjust DPI**: 300 DPI is sweet spot; 400+ for small text, 200 for speed
3. **Parallel pages**: Set `max_workers` (4-8) for multi-page PDFs
4. **Rust extensions**: Always build with `maturin develop --release` for 10-50× speedup on core ops
5. **Table model**: Table Transformer runs on CPU; consider smaller model for speed
6. **Avoid CPU oversubscription**: ONNX Runtime and PyTorch otherwise use every CPU core each. With 4 page workers that becomes `4 × core_count` threads fighting over the cores, so conversion pegs the CPU and can even get slower. By default `intra_threads=0` derives a per-library limit from `cpu_count // max_workers` (e.g. 8 cores / 4 workers → 2 threads each). Override with `--intra-threads N` / `intra_threads` in the GUI. Keep `max_workers × intra_threads ≈ core_count`. For a quick diagnosis, run with `--workers 1`: each library then gets all cores but they no longer compete.
7. **Keep the GUI responsive**: Lower `max_workers` or `intra_threads` if conversion starves the UI thread.

### Why the CPU is at 100%

OCR (ONNX Runtime), Table Transformer (PyTorch) and MuPDF page rendering are CPU-bound computation — holding the image data in RAM avoids disk I/O but does not remove the math. Setting `OMP_NUM_THREADS`/`MKL_NUM_THREADS` and per-library ONNX options caps how many cores each library may use; moving the work off the CPU entirely requires GPU execution (CUDA build of torch + `onnxruntime-gpu`).

## CI/CD

GitHub Actions workflow (`.github/workflows/ci.yml`) includes:
- **Lint**: Rust (fmt, clippy) + Python (ruff, black)
- **Test**: Matrix across Python 3.9-3.12
- **Build Wheels**: Linux manylinux2014 via maturin
- **Publish**: PyPI on release
- **Docker**: Multi-arch build & push to Docker Hub
- **Benchmark**: Performance tracking on main branch

## Roadmap

- [x] Table recognition via Table Transformer
- [x] Figure/image extraction and embedding
- [x] Caption association (figures/tables)
- [x] Header/footer detection
- [x] Parallel page processing
- [x] Docker multi-stage build
- [x] GitHub Actions CI/CD
- [ ] Multi-column layout preservation (text frames)
- [ ] Font embedding for exact visual fidelity
- [ ] Web API (FastAPI) deployment
- [ ] Benchmark suite with regression detection

## License

MIT License - see LICENSE file for details.

## Sponsor

![微信赞赏码](http://qiniu.yhbsoul.com/oneblog/qrcode/20200623133519785.jpg) | ![支付宝收款码](http://qiniu.yhbsoul.com/oneblog/qrcode/20200623133521901.jpg) |