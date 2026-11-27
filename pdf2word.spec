# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Windows GUI build (onedir).

Build with: python -m PyInstaller --clean --noconfirm pdf2word.spec

Output is dist/pdf2word/pdf2word.exe with its dependencies in the same folder
(under _internal). This starts in seconds instead of unpacking ~2.4 GB on every
launch like the previous onefile build.
"""

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

datas = []
binaries = []
hiddenimports = []

# Only packages actually used at runtime. torch/transformers are NOT bundled:
# table recognition installs them on demand into the `deps` folder next to the
# exe (see pdf2word/table_deps.py). surya was removed earlier for the same
# reason -- the pipeline defines its schema dataclasses locally. Dependencies
# are downloaded as plain wheels by table_deps.py, so pip is not needed either.
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
):
    try:
        package_datas, package_binaries, package_imports = collect_all(package)
        datas.extend(package_datas)
        binaries.extend(package_binaries)
        hiddenimports.extend(package_imports)
    except Exception:
        hiddenimports.append(package)

# onnxruntime.tools / onnxruntime.transformers import torch, transformers and
# datasets. Collecting every onnxruntime submodule would silently pull the
# whole heavy chain back into the bundle; only the capi runtime is needed.
# Native DLLs are collected by PyInstaller's hook-onnxruntime.
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
    ]
)

# Never used by this app. Kept as a safeguard if a transitive dependency
# tries to pull them in during analysis.
excludes = [
    # Optional table-recognition stack: installed into deps/ on first use.
    "torch",
    "torchvision",
    "transformers",
    "datasets",
    "gradio",
    "accelerate",
    "huggingface_hub",
    "tokenizers",
    "safetensors",
    # Heavy optional dependencies of the above that this app never uses.
    "bitsandbytes",
    "surya",
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
    name="pdf2word",
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
    name="pdf2word",
)
