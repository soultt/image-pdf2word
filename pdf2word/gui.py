"""Tkinter desktop application for pdf2word."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional, Tuple

from .app_paths import get_app_dir
from .config_store import (
    dict_to_config,
    load_config,
    reset_config,
    save_config,
)
from .history import HistoryDB, MemoryHistory
from .mcp_http import MCPHTTPServer, find_available_port, is_port_bindable
from .pipeline import ConversionConfig, PDF2WordConverter

# (key, label, kind) where kind is int, float, bool, str or password.
PARAM_GROUPS: List[Tuple[str, List[Tuple[str, str, str]]]] = [
    (
        "基础",
        [
            ("dpi", "渲染 DPI", "int"),
            ("max_workers", "并行 worker 数", "int"),
            ("intra_threads", "推理线程数(0=自动)", "int"),
            ("inter_threads", "推理 interop 线程数", "int"),
            ("layout_model", "布局模型标识", "str"),
        ],
    ),
    (
        "OCR 与段落",
        [
            ("ocr_lang", "OCR 语言（+ 分隔）", "str"),
            ("ocr_conf_threshold", "OCR 置信度阈值", "float"),
            ("merge_paragraphs", "合并文本行为段落", "bool"),
            ("para_v_threshold_ratio", "段落合并垂直阈值比", "float"),
            ("preserve_fonts", "保留字号估计", "bool"),
            ("preserve_colors", "保留颜色", "bool"),
        ],
    ),
    (
        "表格与图片",
        [
            ("table_recognition", "表格识别", "bool"),
            ("table_model", "表格识别模型", "str"),
            ("extract_figures", "提取图片", "bool"),
            ("figure_min_size", "最小图片尺寸（像素）", "int"),
        ],
    ),
    (
        "布局 API",
        [
            ("use_layout_api", "使用外部布局 API", "bool"),
            ("layout_api_base", "API Base URL", "str"),
            ("layout_api_key", "API Key", "password"),
            ("layout_api_model", "模型名称", "str"),
        ],
    ),
    (
        "MCP 服务",
        [
            ("mcp_enabled", "启动时启用 MCP", "bool"),
            ("mcp_port", "MCP 端口", "int"),
        ],
    ),
    ("其他", [("nms_iou_threshold", "NMS IoU 阈值", "float")]),
]

PARAM_HELP = {
    "dpi": "渲染 PDF 页面用的分辨率（DPI）。越高越清晰，但越慢、内存占用越大；300 通常足够，小字可用 400。",
    "max_workers": "同时处理页面的线程数。越多越快，但每个 OCR / 表格模型也会占用 CPU 核心；建议与「推理线程数」相乘约等于 CPU 核数。",
    "intra_threads": "每个 OCR / 表格模型内部使用的计算线程数。0 = 自动（CPU 核数 ÷ 并行 worker 数）。",
    "inter_threads": "推理库内部并行阶段的线程数，一般保持 1 即可。",
    "layout_model": "布局模型标识，仅作记录；是否使用布局检测由「使用外部布局 API」开关决定。",
    "ocr_lang": "OCR 识别语言，多个语言用 + 分隔，例如 ch_sim+en。",
    "ocr_conf_threshold": "低于该置信度的 OCR 结果会被丢弃。调高可减少误识别，但可能漏掉模糊文字。",
    "merge_paragraphs": "把相邻的 OCR 文本行合并为段落；关闭则每行单独成段。",
    "para_v_threshold_ratio": "判断两行是否属于同一段落的垂直间距阈值（相对行高）。越大越容易合并。",
    "preserve_fonts": "根据文本框大小估算并保留字号。",
    "preserve_colors": "保留文字颜色；当前版本以默认颜色为主。",
    "table_recognition": "启用表格结构识别并生成 Word 原生表格；关闭时整表按普通文本 OCR。",
    "table_model": "表格结构识别模型（Table Transformer）。首次使用时自动联网下载依赖与模型文件，存放在程序目录的 deps 中。",
    "extract_figures": "检测并裁剪图片区域，作为图片嵌入 Word 文档。",
    "figure_min_size": "小于该尺寸（像素）的区域不会被当作图片提取，用于过滤装饰线条。",
    "use_layout_api": "启用外部 OpenAI 兼容布局 API 检测文本 / 表格 / 图片区域；需同时填写 API 地址和模型名称。",
    "layout_api_base": "外部布局 API 的地址，例如 https://openrouter.ai/api/v1。",
    "layout_api_key": "外部布局 API 的密钥。会以明文保存在 config.json 中，请注意保护该文件。",
    "layout_api_model": "外部布局 API 使用的模型名称，例如 gpt-4o-mini。",
    "mcp_enabled": "GUI 启动时是否同时启动本地 MCP HTTP 服务，供外部工具调用转换。",
    "mcp_port": "MCP 服务监听端口。若端口被占用或被系统策略拒绝，程序会自动切换到可用端口并写回配置。",
    "nms_iou_threshold": "非极大值抑制的 IoU 阈值，用于去除重叠检测框；数值越大保留的框越多。",
}

HISTORY_COLUMNS = (
    ("id", "ID", 55),
    ("started_at", "开始时间", 150),
    ("input_path", "输入 PDF", 260),
    ("output_path", "输出 DOCX", 260),
    ("status", "状态", 80),
    ("duration_ms", "耗时(ms)", 80),
)


class QueueLogHandler(logging.Handler):
    """Forward library log records to the GUI's thread-safe event queue."""

    def __init__(self, events: "queue.Queue[Tuple[str, Any]]") -> None:
        super().__init__(level=logging.INFO)
        self.events = events
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.events.put(("log", self.format(record)))
        except Exception:
            self.handleError(record)


class App:
    """Main window: file selection, parameters, progress and history."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("pdf2word - PDF 转 Word")
        self.root.geometry("1060x760")
        self.root.minsize(900, 640)

        self.events: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self.log_handler = QueueLogHandler(self.events)
        logging.getLogger("pdf2word").addHandler(self.log_handler)
        self.worker: Optional[threading.Thread] = None
        self.input_files: List[str] = []
        self.vars: Dict[str, Any] = {}
        self.history, self.history_error = self._open_history()
        self.mcp_server: Optional[MCPHTTPServer] = None
        self.tray_icon: Any = None
        self._single_instance: Any = None

        self._build_ui()
        self.saved_config = load_config()
        self._apply_config(self.saved_config)
        self._start_mcp(self.saved_config)
        self._setup_tray()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_events)

    @staticmethod
    def _open_history() -> Tuple[Any, Optional[str]]:
        try:
            return HistoryDB(), None
        except Exception as exc:  # SQLite unavailable -> in-memory only
            return MemoryHistory(), str(exc)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)

        notebook = ttk.Notebook(main)
        notebook.pack(fill="both", expand=True)
        self.notebook = notebook

        convert_tab = ttk.Frame(notebook, padding=6)
        history_tab = ttk.Frame(notebook, padding=6)
        notebook.add(convert_tab, text="转换")
        notebook.add(history_tab, text="转换记录")

        self._build_convert_tab(convert_tab)
        self._build_history_tab(history_tab)

    def _build_convert_tab(self, parent: ttk.Frame) -> None:
        file_frame = ttk.LabelFrame(parent, text="PDF 文件", padding=6)
        file_frame.pack(fill="x", pady=(0, 6))

        list_frame = ttk.Frame(file_frame)
        list_frame.pack(side="left", fill="both", expand=True)
        self.file_list = tk.Listbox(list_frame, height=6, selectmode="extended")
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.file_list.yview)
        self.file_list.configure(yscrollcommand=scroll.set)
        self.file_list.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        btn_frame = ttk.Frame(file_frame, padding=(8, 0, 0, 0))
        btn_frame.pack(side="right", fill="y")
        ttk.Button(btn_frame, text="添加", command=self._add_files, width=12).pack(pady=2)
        ttk.Button(btn_frame, text="移除", command=self._remove_files, width=12).pack(pady=2)
        ttk.Button(btn_frame, text="清空", command=self._clear_files, width=12).pack(pady=2)

        out_frame = ttk.LabelFrame(parent, text="输出目录", padding=6)
        out_frame.pack(fill="x", pady=(0, 6))
        self.output_var = tk.StringVar(value=str(get_app_dir()))
        ttk.Entry(out_frame, textvariable=self.output_var).pack(
            side="left", fill="x", expand=True, padx=(0, 6)
        )
        ttk.Button(out_frame, text="浏览", command=self._choose_output, width=10).pack(side="right")

        param_frame = ttk.LabelFrame(parent, text="转换参数", padding=6)
        # Keep the parameter area at a fixed height. Without this, a long MCP
        # error message in a notebook page makes the notebook grow and pushes
        # the action buttons / log panel below the bottom of the window.
        param_frame.pack(fill="x", pady=(0, 6))
        param_notebook = ttk.Notebook(param_frame, height=300)
        param_notebook.pack(fill="both", expand=True)
        for title, fields in PARAM_GROUPS:
            page = ttk.Frame(param_notebook, padding=8)
            param_notebook.add(page, text=title)
            for row, (key, label, kind) in enumerate(fields):
                ttk.Label(page, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
                if kind == "bool":
                    var: Any = tk.BooleanVar(value=False)
                    widget = ttk.Checkbutton(page, variable=var)
                else:
                    var = tk.StringVar(value="")
                    show = "password" if kind == "password" else None
                    widget = ttk.Entry(page, textvariable=var, show=show, width=48)
                self.vars[key] = var
                widget.grid(row=row, column=1, sticky="ew", pady=3)
                ttk.Button(
                    page,
                    text="?",
                    width=2,
                    command=lambda k=key: self._show_param_help(k),
                ).grid(row=row, column=2, sticky="w", padx=(4, 0), pady=3)
            page.columnconfigure(1, weight=1)
            if title == "MCP 服务":
                self.mcp_status_var = tk.StringVar(value="未启动")
                row = len(fields)
                ttk.Label(page, text="状态").grid(
                    row=row, column=0, sticky="w", padx=(0, 10), pady=3
                )
                ttk.Label(
                    page,
                    textvariable=self.mcp_status_var,
                    wraplength=520,
                    justify="left",
                ).grid(row=row, column=1, sticky="w", pady=3)
                ttk.Button(
                    page, text="应用并重启 MCP", command=self._apply_mcp_settings
                ).grid(row=row + 1, column=1, sticky="w", pady=6)

        action_frame = ttk.Frame(parent)
        action_frame.pack(fill="x", pady=(0, 6))
        self.start_button = ttk.Button(
            action_frame, text="开始转换", command=self.start_conversion
        )
        self.start_button.pack(side="left")
        ttk.Button(action_frame, text="保存配置", command=self._save_current_config).pack(
            side="left", padx=6
        )
        ttk.Button(action_frame, text="恢复默认", command=self._restore_defaults).pack(
            side="left"
        )
        self.progress = ttk.Progressbar(action_frame, mode="determinate", length=260)
        self.progress.pack(side="right", padx=6)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(action_frame, textvariable=self.status_var, width=26).pack(side="right")

        log_frame = ttk.LabelFrame(parent, text="日志", padding=6)
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, height=9, wrap="word")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")
        self._log("配置与历史文件位于程序所在目录。")
        if self.history_error:
            self._log(f"历史数据库不可用，使用内存记录: {self.history_error}")

    def _build_history_tab(self, parent: ttk.Frame) -> None:
        search_frame = ttk.Frame(parent)
        search_frame.pack(fill="x", pady=(0, 6))
        ttk.Label(search_frame, text="搜索:").pack(side="left")
        self.query_var = tk.StringVar()
        ttk.Entry(search_frame, textvariable=self.query_var, width=36).pack(
            side="left", padx=(4, 10)
        )
        ttk.Label(search_frame, text="状态:").pack(side="left")
        self.status_filter = ttk.Combobox(
            search_frame, values=["全部", "success", "failed", "running"], width=12
        )
        self.status_filter.current(0)
        self.status_filter.pack(side="left", padx=(4, 10))
        ttk.Button(search_frame, text="查询", command=self.refresh_history).pack(side="left")
        ttk.Button(search_frame, text="刷新", command=self.refresh_history).pack(
            side="left", padx=4
        )
        ttk.Button(search_frame, text="清空记录", command=self._clear_history).pack(
            side="left"
        )

        tree_frame = ttk.Frame(parent)
        tree_frame.pack(fill="both", expand=True)
        self.history_tree = ttk.Treeview(
            tree_frame,
            columns=[column[0] for column in HISTORY_COLUMNS],
            show="headings",
            height=16,
        )
        for key, heading, width in HISTORY_COLUMNS:
            self.history_tree.heading(key, text=heading)
            self.history_tree.column(key, width=width, anchor="w")
        tree_scroll = ttk.Scrollbar(
            tree_frame, orient="vertical", command=self.history_tree.yview
        )
        self.history_tree.configure(yscrollcommand=tree_scroll.set)
        self.history_tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

        action_frame = ttk.Frame(parent)
        action_frame.pack(fill="x", pady=(6, 0))
        ttk.Button(action_frame, text="打开输出", command=self._open_output).pack(side="left")
        ttk.Button(action_frame, text="重试", command=self._retry_record).pack(
            side="left", padx=6
        )
        ttk.Button(action_frame, text="删除记录", command=self._delete_record).pack(side="left")

    # -------------------------------------------------------------- helpers

    def _log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _show_param_help(self, key: str) -> None:
        """Show a short explanation when the '?' next to a parameter is clicked."""
        label = next(
            (label for _, fields in PARAM_GROUPS for k, label, _ in fields if k == key),
            key,
        )
        messagebox.showinfo(
            f"参数说明 - {label}",
            PARAM_HELP.get(key, "暂无说明。"),
            parent=self.root,
        )

    def _add_files(self) -> None:
        selected = filedialog.askopenfilenames(
            title="选择 PDF 文件", filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")]
        )
        for name in selected:
            if name not in self.input_files:
                self.input_files.append(name)
                self.file_list.insert("end", name)

    def _remove_files(self) -> None:
        for index in reversed(self.file_list.curselection()):
            self.input_files.pop(index)
            self.file_list.delete(index)

    def _clear_files(self) -> None:
        self.input_files.clear()
        self.file_list.delete(0, "end")

    def _choose_output(self) -> None:
        chosen = filedialog.askdirectory(title="选择输出目录")
        if chosen:
            self.output_var.set(chosen)

    def _apply_config(self, config: ConversionConfig) -> None:
        values = {
            key: getattr(config, key)
            for group in PARAM_GROUPS
            for key, _, _ in group[1]
        }
        for key, var in self.vars.items():
            if key in values:
                var.set(values[key])

    def _collect_config(self) -> Optional[ConversionConfig]:
        values: Dict[str, Any] = {}
        for group in PARAM_GROUPS:
            for key, label, kind in group[1]:
                raw = self.vars[key].get()
                try:
                    if kind == "bool":
                        values[key] = bool(raw)
                    elif kind == "int":
                        values[key] = int(str(raw).strip())
                    elif kind == "float":
                        values[key] = float(str(raw).strip())
                    else:
                        values[key] = str(raw)
                except (TypeError, ValueError):
                    messagebox.showerror("参数错误", f"参数“{label}”的值无效: {raw}")
                    return None
        try:
            config = dict_to_config(values)
            if config.layout_api_base and not config.layout_api_model:
                raise ValueError("设置了布局 API Base URL 时必须同时填写模型名称")
            return config
        except (TypeError, ValueError) as exc:
            messagebox.showerror("参数错误", str(exc))
            return None

    def _save_current_config(self) -> None:
        config = self._collect_config()
        if config is None:
            return
        save_config(config)
        self._log("配置已保存到程序所在目录的 config.json")

    def _restore_defaults(self) -> None:
        self._apply_config(reset_config())
        self._log("已恢复默认配置")

    # ------------------------------------------------------------------ MCP

    def _set_mcp_status(self, text: str) -> None:
        if hasattr(self, "mcp_status_var"):
            self.mcp_status_var.set(text)

    @staticmethod
    def _short_error(text: Optional[str], limit: int = 160) -> str:
        """One concise line for the status label; the full text goes to the log."""
        if not text:
            return "未知错误"
        lines = [line.strip() for line in str(text).splitlines() if line.strip()]
        keys = ("PermissionError", "OSError", "WinError", "error while attempting", "Address already in use")
        detail = next((line for line in lines if any(k in line for k in keys)), lines[0])
        detail = " ".join(detail.split())
        return detail if len(detail) <= limit else detail[:limit] + "…"

    def _start_mcp(self, config: ConversionConfig) -> None:
        if self.mcp_server is not None:
            self.mcp_server.stop()
            self.mcp_server = None
        if not config.mcp_enabled:
            self._set_mcp_status("已禁用")
            return

        port = int(config.mcp_port)
        if not 1 <= port <= 65535:
            self._set_mcp_status("启动失败: 端口无效")
            self._log(f"MCP 服务启动失败: 无效端口 {config.mcp_port}")
            return

        # Windows may reject a port with WinError 10013 even when nothing
        # listens on it (reserved by another service / security software).
        # Fall back to a bindable port and persist it so MCP clients that read
        # config.json connect to the right address.
        if not is_port_bindable("127.0.0.1", port):
            fallback = find_available_port("127.0.0.1", preferred=8765)
            self._log(
                f"MCP 端口 {port} 无法绑定（被占用或被系统策略阻止），自动改用 {fallback}"
            )
            port = fallback
            config.mcp_port = port
            if "mcp_port" in self.vars:
                self.vars["mcp_port"].set(str(port))
            save_config(config)
            self.saved_config = config

        try:
            server = MCPHTTPServer(port=port)
            error = server.start()
        except Exception as exc:
            error = str(exc)
            server = None
        if error or server is None:
            self._set_mcp_status(f"启动失败: {self._short_error(error)}")
            self._log(f"MCP 服务启动失败: {error}")
            return
        self.mcp_server = server
        url = f"http://127.0.0.1:{port}/mcp"
        self._set_mcp_status(f"运行中: {url}")
        self._log(f"MCP 服务已启动: {url}")

    def _apply_mcp_settings(self) -> None:
        config = self._collect_config()
        if config is None:
            return
        if not 1 <= config.mcp_port <= 65535:
            messagebox.showerror("参数错误", "MCP 端口必须在 1-65535 之间")
            return
        save_config(config)
        self.saved_config = config
        self._start_mcp(config)

    # ------------------------------------------------------------------ tray

    def _setup_tray(self) -> None:
        """Put an icon in the system tray with show/quit actions."""
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception as exc:
            self._log(f"系统托盘不可用: {exc}")
            return

        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((4, 4, 60, 60), radius=10, fill=(41, 98, 155, 255))
        draw.rectangle((18, 28, 46, 32), fill="white")
        draw.rectangle((18, 36, 38, 40), fill="white")
        menu = pystray.Menu(
            pystray.MenuItem("显示窗口", self._request_show, default=True),
            pystray.MenuItem("退出", self._request_quit),
        )
        try:
            icon = pystray.Icon("pdf2word", image, "pdf2word - PDF 转 Word", menu)
            self.tray_icon = icon
            threading.Thread(
                target=icon.run, daemon=True, name="pdf2word-tray"
            ).start()
            self._log("已添加系统托盘图标；关闭窗口会隐藏到托盘")
        except Exception as exc:
            self.tray_icon = None
            self._log(f"系统托盘初始化失败: {exc}")

    def attach_single_instance(self, instance: Any) -> None:
        """Watch for a second launch and restore the window when signalled."""
        self._single_instance = instance

        def watch() -> None:
            while True:
                instance.wait_for_show()
                self.events.put(("show", None))

        threading.Thread(
            target=watch, daemon=True, name="pdf2word-single-instance"
        ).start()

    def _request_show(self, *_args: Any) -> None:
        """Called from the tray thread: marshal onto the Tk thread."""
        self.events.put(("show", None))

    def _request_quit(self, *_args: Any) -> None:
        """Called from the tray thread: marshal onto the Tk thread."""
        self.events.put(("quit", None))

    def _show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(250, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def _quit_app(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askokcancel("确认", "转换仍在进行，确定退出吗？"):
                return
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None
        if self.mcp_server is not None:
            self.mcp_server.stop()
        logging.getLogger("pdf2word").removeHandler(self.log_handler)
        self.log_handler.close()
        if self._single_instance is not None:
            try:
                self._single_instance.close()
            except Exception:
                pass
        self.root.destroy()

    # ------------------------------------------------------------ conversion

    def start_conversion(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            messagebox.showinfo("提示", "转换正在进行中")
            return
        if not self.input_files:
            messagebox.showwarning("提示", "请先添加 PDF 文件")
            return

        config = self._collect_config()
        if config is None:
            return

        output_dir = Path(self.output_var.get().strip() or str(get_app_dir()))
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            save_config(config)
        except OSError as exc:
            messagebox.showerror("错误", f"无法准备输出目录或保存配置: {exc}")
            return

        self.start_button.configure(state="disabled")
        self.progress["value"] = 0
        self.status_var.set("启动中...")
        self.worker = threading.Thread(
            target=self._run_batch,
            args=(list(self.input_files), output_dir, config),
            daemon=True,
        )
        self.worker.start()

    def _run_batch(
        self, files: List[str], output_dir: Path, config: ConversionConfig
    ) -> None:
        try:
            converter = PDF2WordConverter(config)
        except Exception as exc:  # pragma: no cover - defensive
            self.events.put(("log", f"初始化转换器失败: {exc}"))
            self.events.put(("finished", None))
            return

        total = len(files)
        params = {
            key: getattr(config, key)
            for group in PARAM_GROUPS
            for key, _, _ in group[1]
        }
        for index, pdf_path in enumerate(files, 1):
            source = Path(pdf_path)
            target = output_dir / (source.stem + ".docx")
            self.events.put(("log", f"[{index}/{total}] {source.name} -> {target.name}"))

            def _on_page(done: int, pages: int) -> None:
                self.events.put(("page_progress", (done, pages)))

            def _on_warning(message: str) -> None:
                self.events.put(("log", f"  警告: {message}"))

            record_id = self.history.add_conversion(source, target, params)
            started = time.perf_counter()
            status = "success"
            error = None
            try:
                converter.convert(
                    source,
                    target,
                    progress_callback=_on_page,
                    warning_callback=_on_warning,
                )
                self.events.put(("log", f"  转换成功 ({index}/{total})"))
            except Exception as exc:
                status = "failed"
                error = str(exc)
                self.events.put(("log", f"  转换失败 ({index}/{total}): {exc}"))
            duration_ms = int((time.perf_counter() - started) * 1000)
            self.history.finish_conversion(record_id, status, duration_ms, error)
            self.events.put(("progress", (index, total, source.name)))
        self.events.put(("finished", None))

    def _poll_events(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._log(str(payload))
            elif kind == "progress":
                index, total, name = payload
                self.progress["value"] = 100.0 * index / total
                self.status_var.set(f"{name} ({index}/{total})")
            elif kind == "page_progress":
                done, pages = payload
                pct = 100.0 * done / pages if pages else 0.0
                self._log(f"    页面进度 {done}/{pages} ({pct:.0f}%)")
                self.status_var.set(f"页面 {done}/{pages}")
            elif kind == "finished":
                self.progress["value"] = 100
                self.status_var.set("转换结束")
                self.start_button.configure(state="normal")
                self.refresh_history()
            elif kind == "show":
                self._show_window()
            elif kind == "quit":
                self._quit_app()
        self.root.after(100, self._poll_events)

    # ---------------------------------------------------------------- history

    def refresh_history(self) -> None:
        if not hasattr(self, "history_tree"):
            return
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        status = self.status_filter.get()
        status = None if status in ("", "全部") else status
        rows = self.history.search(self.query_var.get(), status)
        for row in rows:
            self.history_tree.insert(
                "",
                "end",
                iid=str(row["id"]),
                values=(
                    row["id"],
                    row["started_at"],
                    row["input_path"],
                    row["output_path"],
                    row["status"],
                    row["duration_ms"],
                ),
            )

    def _selected_record(self) -> Optional[Dict[str, Any]]:
        selection = self.history_tree.selection()
        if not selection:
            messagebox.showinfo("提示", "请先选择一条转换记录")
            return None
        return self.history.get(int(selection[0]))

    def _open_output(self) -> None:
        record = self._selected_record()
        if record is None:
            return
        output = Path(record["output_path"])
        if not output.exists():
            messagebox.showwarning("提示", "输出文件不存在")
            return
        try:
            import os

            os.startfile(str(output))  # noqa: S606 - Windows desktop integration
        except Exception as exc:
            messagebox.showerror("错误", f"无法打开文件: {exc}")

    def _retry_record(self) -> None:
        record = self._selected_record()
        if record is None:
            return
        try:
            params = json.loads(record["params_json"] or "{}")
            config = dict_to_config(params)
        except (ValueError, TypeError) as exc:
            messagebox.showerror("错误", f"记录中的参数无效: {exc}")
            return
        self._apply_config(config)
        self.input_files = [record["input_path"]]
        self.file_list.delete(0, "end")
        self.file_list.insert("end", record["input_path"])
        target = Path(record["output_path"])
        self.output_var.set(str(target.parent))
        self.notebook.select(0)
        self.start_conversion()

    def _delete_record(self) -> None:
        selection = self.history_tree.selection()
        if not selection:
            messagebox.showinfo("提示", "请先选择一条转换记录")
            return
        self.history.delete(int(selection[0]))
        self.refresh_history()

    def _clear_history(self) -> None:
        if messagebox.askokcancel("确认", "确定清空所有转换记录吗？"):
            self.history.clear()
            self.refresh_history()

    def _on_close(self) -> None:
        # With a tray icon, closing the window hides it to the tray instead of
        # exiting; use the tray menu "退出" to quit (it asks if a job is running).
        if self.tray_icon is not None:
            self.root.withdraw()
            return
        self._quit_app()


def main(single_instance: Any = None) -> None:
    """Launch the GUI; ``single_instance`` enables restore-on-second-launch."""
    root = tk.Tk()
    app = App(root)
    if single_instance is not None:
        app.attach_single_instance(single_instance)
    try:
        root.mainloop()
    finally:
        if single_instance is not None:
            try:
                single_instance.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
