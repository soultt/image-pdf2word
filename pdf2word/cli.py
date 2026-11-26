"""Command-line interface for pdf2word."""

import logging
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.progress import Progress, TextColumn
from rich.logging import RichHandler

from .pipeline import pdf_to_docx, ConversionConfig, PDF2WordConverter

console = Console()


def setup_logging(verbose: bool = False, quiet: bool = False):
    """Configure logging."""
    level = logging.WARNING
    if verbose:
        level = logging.DEBUG
    elif quiet:
        level = logging.ERROR
    else:
        level = logging.INFO

    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


@click.group()
@click.version_option(version="0.1.0", prog_name="pdf2word")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging")
@click.option("-q", "--quiet", is_flag=True, help="Suppress non-error output")
@click.pass_context
def cli(ctx: click.Context, verbose: bool, quiet: bool):
    """Convert image-based PDF to editable Word documents with OCR and layout analysis."""
    setup_logging(verbose, quiet)
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    ctx.obj["quiet"] = quiet


@cli.command()
@click.argument("input_pdf", type=click.Path(exists=True, path_type=Path))
@click.argument("output_docx", type=click.Path(path_type=Path))
@click.option("--dpi", default=300, show_default=True, help="Render DPI for OCR")
@click.option("--lang", default="ch_sim+en", show_default=True, help="OCR languages, plus-separated (e.g. ch_sim+en)")
@click.option("--conf-threshold", default=0.5, show_default=True, help="OCR confidence threshold")
@click.option("--no-table", is_flag=True, help="Disable table recognition")
@click.option("--table-model", default="microsoft/table-transformer-structure-recognition", show_default=True, help="Table structure recognition model")
@click.option("--no-merge", is_flag=True, help="Don't merge text lines into paragraphs")
@click.option("--para-threshold", default=0.5, show_default=True, help="Paragraph merge vertical threshold ratio")
@click.option("--no-figures", is_flag=True, help="Disable figure/image extraction")
@click.option("--figure-min-size", default=50, show_default=True, help="Minimum figure size in pixels")
@click.option("--workers", default=4, show_default=True, help="Parallel page workers")
@click.option("--intra-threads", default=0, show_default=True, help="Threads per OCR/table inference library; 0=auto (CPU cores / workers)")
@click.option("--use-layout-api", is_flag=True, help="Use external layout API (requires --layout-api-base and --layout-api-model)")
# External API options for layout detection
@click.option("--layout-api-base", default="", show_default=True, help="OpenAI-compatible API base URL. Free options: 'https://generativelanguage.googleapis.com/v1beta/openai/' (Google) or 'https://openrouter.ai/api/v1' (register at openrouter.ai/keys)")
@click.option("--layout-api-key", default="", show_default=True, help="API key. Get FREE Google key: aistudio.google.com/app/apikey | Get OpenRouter key: openrouter.ai/keys")
@click.option("--layout-api-model", default="", show_default=True, help="Model ID, e.g. gemini-2.0-flash-exp (Google) or google/gemini-2.0-flash (OpenRouter)")
@click.pass_context
def convert(
    ctx: click.Context,
    input_pdf: Path,
    output_docx: Path,
    dpi: int,
    lang: str,
    conf_threshold: float,
    no_table: bool,
    table_model: str,
    no_merge: bool,
    para_threshold: float,
    no_figures: bool,
    figure_min_size: int,
    workers: int,
    intra_threads: int,
    use_layout_api: bool,
    layout_api_base: str,
    layout_api_key: str,
    layout_api_model: str,
):
    """Convert a single PDF file to Word document."""
    config = ConversionConfig(
        dpi=dpi,
        ocr_lang=lang,
        ocr_conf_threshold=conf_threshold,
        table_recognition=not no_table,
        table_model=table_model,
        merge_paragraphs=not no_merge,
        para_v_threshold_ratio=para_threshold,
        extract_figures=not no_figures,
        figure_min_size=figure_min_size,
        max_workers=workers,
        intra_threads=intra_threads,
        layout_api_base=layout_api_base,
        layout_api_key=layout_api_key,
        layout_api_model=layout_api_model,
        use_layout_api=use_layout_api,
    )

    # Warn if API mode without key (OpenRouter requires key)
    if layout_api_base and not layout_api_key:
        console.print(f"[yellow]NOTE:[/yellow] No --layout-api-key provided. Free models like Gemini Flash may require an OpenRouter key.")
        console.print(f"  Get free key: https://openrouter.ai/keys")
        console.print()

    console.print(f"[bold]Converting:[/bold] {input_pdf} -> {output_docx}")

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Processing...", total=None)

        try:
            pdf_to_docx(input_pdf, output_docx, config)
            progress.update(task, description="[green]Done![/green]")
            console.print(f"[green]OK[/green] Saved to {output_docx}")
        except Exception as e:
            progress.update(task, description="[red]Failed[/red]")
            console.print(f"[red]ERROR[/red] Error: {e}")
            if ctx.obj.get("verbose"):
                console.print_exception()
            sys.exit(1)


@cli.command()
@click.argument("input_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.argument("output_dir", type=click.Path(path_type=Path))
@click.option("--dpi", default=300, show_default=True)
@click.option("--conf-threshold", default=0.5, show_default=True)
@click.option("--lang", default="ch_sim+en", show_default=True, help="OCR languages, plus-separated (e.g. ch_sim+en)")
@click.option("--pattern", default="*.pdf", show_default=True, help="File pattern to match")
@click.option("--recursive/--no-recursive", default=False, show_default=True)
@click.option("--workers", default=4, show_default=True, help="Parallel page workers")
@click.option("--intra-threads", default=0, show_default=True, help="Threads per OCR/table inference library; 0=auto (CPU cores / workers)")
@click.option("--use-layout-api", is_flag=True, help="Use external layout API (requires --layout-api-base and --layout-api-model)")
@click.option("--no-table", is_flag=True, help="Disable table recognition")
@click.option("--table-model", default="microsoft/table-transformer-structure-recognition", show_default=True)
@click.option("--no-figures", is_flag=True, help="Disable figure/image extraction")
@click.option("--figure-min-size", default=50, show_default=True, help="Minimum figure size in pixels")
# External API options for layout detection
@click.option("--layout-api-base", default="", show_default=True, help="OpenAI-compatible API base URL. Free options: 'https://generativelanguage.googleapis.com/v1beta/openai/' (Google) or 'https://openrouter.ai/api/v1' (register at openrouter.ai/keys)")
@click.option("--layout-api-key", default="", show_default=True, help="API key. Get FREE Google key: aistudio.google.com/app/apikey | Get OpenRouter key: openrouter.ai/keys")
@click.option("--layout-api-model", default="", show_default=True, help="Model ID, e.g. gemini-2.0-flash-exp (Google) or google/gemini-2.0-flash (OpenRouter)")
@click.pass_context
def batch(
    ctx: click.Context,
    input_dir: Path,
    output_dir: Path,
    dpi: int,
    lang: str,
    conf_threshold: float,
    pattern: str,
    recursive: bool,
    workers: int,
    intra_threads: int,
    no_table: bool,
    table_model: str,
    no_figures: bool,
    figure_min_size: int,
    use_layout_api: bool,
    layout_api_base: str,
    layout_api_key: str,
    layout_api_model: str,
):
    """Batch convert multiple PDF files."""
    output_dir.mkdir(parents=True, exist_ok=True)

    if recursive:
        files = list(input_dir.rglob(pattern))
    else:
        files = list(input_dir.glob(pattern))

    if not files:
        console.print(f"[yellow]No files matching '{pattern}' found in {input_dir}[/yellow]")
        return

    config = ConversionConfig(
        dpi=dpi,
        ocr_lang=lang,
        ocr_conf_threshold=conf_threshold,
        table_recognition=not no_table,
        table_model=table_model,
        extract_figures=not no_figures,
        figure_min_size=figure_min_size,
        max_workers=workers,
        intra_threads=intra_threads,
        layout_api_base=layout_api_base,
        layout_api_key=layout_api_key,
        layout_api_model=layout_api_model,
        use_layout_api=use_layout_api,
    )

    converter = PDF2WordConverter(config)

    console.print(f"[bold]Batch converting {len(files)} files...[/bold]")

    with Progress(console=console) as progress:
        task = progress.add_task("Converting", total=len(files))

        for pdf_file in files:
            rel_path = pdf_file.relative_to(input_dir)
            out_file = output_dir / rel_path.with_suffix(".docx")
            out_file.parent.mkdir(parents=True, exist_ok=True)

            progress.update(task, description=f"Processing {pdf_file.name}")

            try:
                converter.convert(pdf_file, out_file)
                console.print(f"  [green]+[/green] {rel_path} -> {out_file.relative_to(output_dir)}")
            except Exception as e:
                console.print(f"  [red]-[/red] {rel_path}: {e}")
                if ctx.obj.get("verbose"):
                    console.print_exception()

            progress.advance(task)

    console.print("[bold green]Batch conversion complete![/bold green]")


@cli.command()
@click.option("--test-rust/--no-test-rust", default=True, help="Test Rust bindings")
def doctor(test_rust: bool):
    """Check installation and dependencies."""
    console.print("[bold]pdf2word Environment Check[/bold]\n")

    # Python version
    import sys
    console.print(f"Python: {sys.version.split()[0]}")

    # Key dependencies
    deps = [
        ("pymupdf", "fitz"),
        ("rapidocr-onnxruntime", "rapidocr_onnxruntime"),
        ("python-docx", "docx"),
        ("opencv-python-headless", "cv2"),
        ("numpy", "numpy"),
        ("pillow", "PIL"),
        ("lxml", "lxml"),
        ("beautifulsoup4", "bs4"),
    ]

    for pkg, mod in deps:
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", "unknown")
            console.print(f"  [green]+[/green] {pkg}: {ver}")
        except ImportError:
            console.print(f"  [red]-[/red] {pkg}: NOT INSTALLED")

    # Optional table-recognition dependencies are installed on demand.
    from .table_deps import available as table_deps_available
    from .app_paths import get_deps_dir

    if table_deps_available():
        console.print("  [green]+[/green] table deps (torch/transformers): available")
    else:
        console.print(
            "  [yellow]![/yellow] table deps (torch/transformers): not installed "
            f"(auto-downloaded into {get_deps_dir()} on first table conversion)"
        )

    # Rust bindings
    if test_rust:
        try:
            import pdf2word_ops
            console.print(f"  [green]+[/green] pdf2word_ops (Rust): loaded")
            # Quick functional test
            import numpy as np
            arr = np.array([[100, 100, 200, 200]], dtype=np.float32)
            result = pdf2word_ops.coords_px_to_pt(arr, 300.0)
            expected = 100 * 72 / 300
            assert abs(result[0, 0] - expected) < 0.01
            console.print(f"  [green]+[/green] Rust bindings functional test passed")
        except ImportError:
            console.print(f"  [yellow]![/yellow] pdf2word_ops: NOT BUILT (run 'maturin develop')")
        except Exception as e:
            console.print(f"  [red]-[/red] pdf2word_ops: ERROR - {e}")

    console.print("\n[bold]Done.[/bold]")


def main():
    cli(obj={})


if __name__ == "__main__":
    main()