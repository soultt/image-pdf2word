# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the FULL Windows GUI build (onedir).

Unlike pdf2word.spec, this build bundles the table-recognition stack
(torch + transformers) so no dependency download is ever needed.

Build with: python -m PyInstaller --clean --noconfirm pdf2word-full.spec

Output is dist/pdf2word-full/pdf2word-full.exe (next to the slim build).
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

datas = []

# Bundle the Table Transformer weights/config so the full build never needs
# to download from Hugging Face (and never hits requests/certifi SSL issues).
model_dir = Path("models/table-transformer-structure-recognition")
if model_dir.is_dir():
    for model_file in sorted(model_dir.glob("*")):
        if model_file.is_file():
            datas.append(
                (str(model_file), "models/table-transformer-structure-recognition")
            )
binaries = []
hiddenimports = []

# The full build deliberately includes the table-recognition dependencies.
for package in (
    "pdf2word_ops",
    "rapidocr_onnxruntime",
    "cv2",
    "fitz",
    "pymupdf",
    "docx",
    "pystray",
    "lxml",
    "uvicorn",
    "starlette",
    "anyio",
    "mcp",
    "torch",
    "torchvision",
    "transformers",
    "huggingface_hub",
    "tokenizers",
    "safetensors",
    "timm",
):
    try:
        package_datas, package_binaries, package_imports = collect_all(package)
        datas.extend(package_datas)
        binaries.extend(package_binaries)
        hiddenimports.extend(package_imports)
    except Exception:
        hiddenimports.append(package)

# Only the onnxruntime capi runtime is needed; tools/transformers submodules
# would otherwise drag in an even larger optional dependency chain.
datas.extend(collect_data_files("onnxruntime"))
hiddenimports.extend(
    collect_submodules(
        "onnxruntime",
        filter=lambda name: name == "onnxruntime" or name.startswith("onnxruntime.capi"),
    )
)

hiddenimports.extend(
    [
        "app_single_instance",
        "pystray._win32",
        "pdf2word",
        "pdf2word.gui",
        "pdf2word.pipeline",
        "pdf2word.ops",
        "pdf2word.app_paths",
        "pdf2word.config_store",
        "pdf2word.history",
        "pdf2word.mcp_http",
        "pdf2word.mcp_server",
        "pdf2word.table_deps",
        "tkinter",
        "numpy",
        "PIL",
        "fitz",
        "pymupdf",
        "pymupdf.mupdf",
        "uvicorn.logging",
        # Dynamic model / processor loading.
        "transformers.models.table_transformer.modeling_table_transformer",
        "transformers.models.table_transformer.configuration_table_transformer",
        "transformers.models.detr.image_processing_detr",
        "transformers.image_processing_utils",
        "transformers.modeling_utils",
    ]
)

# Optional dependencies of transformers/torch that this app never uses.
excludes = [
    "bitsandbytes",
    "surya",
    "datasets",
    "gradio",
    "accelerate",
    "sklearn",
    "scipy",
    "librosa",
    "numba",
    "llvmlite",
    "pyarrow",
    "pandas",
    "imageio",
    "imageio_ffmpeg",
    "triton",
]

a = Analysis(
    ["gui_main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pdf2word-full",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="pdf2word-full",
)
