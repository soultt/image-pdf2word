# pdf2word

[English](README.md) | 简体中文

通过 OCR、版面分析和结构还原，将图片型（扫描版）PDF 转换为可编辑的 Word 文档（.docx）。

## 功能特性

- **OCR**：基于 RapidOCR（ONNX Runtime）的多语言识别 —— 中文、英文及 80 多种语言
- **版面分析**：配置后使用外部版面 API；未配置时跳过版面模型，整页按单个文本块进行 OCR
- **阅读顺序**：针对多栏布局的智能拓扑排序
- **表格识别**：Table Transformer（Microsoft）识别结构 + 单元格级 OCR → 生成原生 docx 表格，支持 rowspan/colspan
- **图片提取**：高质量裁剪并嵌入图片，自动关联图注
- **图注关联**：自动将图注与图片/表格关联
- **页眉/页脚检测**：基于区域的检测与专门处理
- **高性能**：核心算子（坐标变换、NMS、排序）通过 PyO3 使用 Rust 实现
- **并行处理**：多线程页面处理，工作线程数可配置
- **批量处理**：并行转换多个 PDF
- **可配置**：DPI、语言、置信度阈值、段落合并
- **Docker 支持**：多阶段构建，适用于生产部署
- **Windows 桌面应用**：Tkinter 桌面程序，带参数面板和转换历史

## 安装

### 前置条件

- Python 3.9+
- Rust 工具链（用于构建核心算子）：`curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`
- ONNX Runtime（已通过 `rapidocr-onnxruntime` 包含）

### 开发环境安装

```bash
# 克隆并进入目录
cd pdf2word

# 安装 Python 依赖
pip install -e ".[dev]"

# 构建 Rust 扩展（需要 maturin）
maturin develop --release

# 验证安装
python -m pdf2word doctor
```

### 快速安装（无需 Rust）

所有 Rust 算子都有纯 Python 回退实现：

```bash
pip install -e .
python -m pdf2word doctor --no-test-rust
```

常规 OCR 转换**不需要** PyTorch 和 Transformers。它们仅在表格结构识别时用到，首次使用时按需安装到 `deps/` 目录（参见[表格识别依赖](#表格识别依赖)）。若要在源码环境中预先安装：

```bash
pip install -e ".[table]"
```

### Docker 安装

```bash
# 构建镜像
docker build -t pdf2word .

# 运行转换
docker run --rm -v $(pwd)/input:/app/input:ro -v $(pwd)/output:/app/output \
  pdf2word convert /app/input/document.pdf /app/output/document.docx

# 或使用 docker-compose
docker-compose up pdf2word
```

## 使用方法

### 命令行

```bash
# 单个文件
pdf2word convert input.pdf output.docx

# 带参数
pdf2word convert input.pdf output.docx --dpi 300 --lang ch_sim+en --conf-threshold 0.6

# 表格识别相关参数
pdf2word convert input.pdf output.docx --table-model microsoft/table-transformer-structure-recognition --no-table

# 图片提取相关参数
pdf2word convert input.pdf output.docx --no-figures --figure-min-size 50

# 并行处理
pdf2word convert input.pdf output.docx --workers 4

# 外部版面 API（显式开关，两个参数必须同时提供）
pdf2word convert input.pdf output.docx --use-layout-api \
  --layout-api-base https://api.openai.com/v1 --layout-api-model gpt-4o-mini

# 批量转换
pdf2word batch ./pdfs ./output --recursive --pattern "*.pdf"

# 检查环境
pdf2word doctor
```

### Windows 桌面应用

Windows 上提供 Tkinter 图形界面（命令行用法不变）：

```bash
# 从源码运行
python -m pdf2word gui
# 等价命令：python -m pdf2word.gui

# 构建 Windows 应用（onedir 模式）
build_windows.bat
# 输出：dist/pdf2word/pdf2word.exe（需保留整个 dist/pdf2word 目录）
```

GUI 支持添加多个 PDF、选择输出目录、编辑全部转换参数、显示进度/日志，以及带搜索、打开输出和重试操作的历史记录页。它会在系统托盘放置图标：关闭窗口会隐藏到托盘，托盘菜单提供“显示窗口”和“退出”。同一安装目录只允许运行一个实例 —— 再次启动会通知正在运行的实例恢复/聚焦其窗口，而不会启动第二个副本。

`config.json` 和 `history.db` 存放在**应用程序旁边**：打包版位于 `pdf2word.exe` 所在目录，源码运行时位于项目根目录。可通过 `PDF2WORD_APP_DIR` 覆盖该位置。API 密钥以明文保存在 `config.json` 中，如保存了真实密钥请注意保护该文件。PyInstaller 打包体积较大，因为包含 ONNX Runtime 和 Rust 扩展；**不再打包 torch / transformers** —— 它们会在首次进行表格识别时下载到 `dist/pdf2word/deps/`。打包采用 **onedir** 模式：启动程序位于 `dist/pdf2word/`，依赖文件放在同一目录，因此启动只需几秒，不必每次解压约 2 GB 的 onefile 压缩包。分发时将该目录打包即可，并让 `config.json`、`history.db` 和 `deps/` 与 exe 保持同级。无用的间接依赖（bitsandbytes、surya、pyarrow、numba）由 `pdf2word.spec` 排除。

构建需要 PyInstaller（`build_windows.bat` 会自动安装）。

### MCP 服务（HTTP）

Windows 可执行程序在 GUI 启动时会自动开启 HTTP MCP 服务（可在 GUI 的“MCP 服务”页配置）。源码运行方式在启动 GUI 时同样会开启。连接方式：

```json
{
  "mcpServers": {
    "pdf2word": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

端口和启用开关保存在应用程序旁的 `config.json` 中；GUI 可通过“应用并重启 MCP”修改。服务仅绑定 localhost。

唯一的工具是 `convert_pdf(pdf_path, output_path=None, dpi=None, lang=None, conf_threshold=None, max_workers=None)`，返回 `ok`、`output_path`、`duration_ms` 和 `error`。

API 配置**不**通过 MCP 管理：`use_layout_api` 和 `layout_api_base/key/model` 从 `config.json` 读取（由 GUI、CLI 写入或直接编辑）。MCP 不提供读取或修改配置的工具。

### 版面检测模式

版面检测由显式的 `--use-layout-api` 参数控制（GUI 中对应复选框），而非由是否填写 base URL 决定。

- **默认（`use_layout_api=False`）**：不加载版面模型。整页按单个文本块 OCR，因此不会单独检测表格、图片和图注，也不保留多栏阅读顺序。
- **启用（`--use-layout-api`）**：必须同时提供 `--layout-api-base` 和 `--layout-api-model`，否则会因配置错误而转换失败。外部 OpenAI 兼容的版面 API 会检测文本/标题/表格/图片/图注区块。

### 表格识别依赖

Table Transformer（PyTorch + Transformers）是可选组件，**不包含**在打包应用中。首次真正识别到表格区块时，转换器会在 `pdf2word.exe` 旁的 `deps/` 目录（源码运行时为项目根目录）后台下载依赖；该次转换中表格会回退为纯文本 OCR，下一次转换才会使用 Table Transformer。进度和错误会显示在 GUI 日志面板（或 CLI 日志）中。详情：

- 来自 `download.pytorch.org/whl/cpu` 的纯 CPU 版 PyTorch wheel（约 200 MB），以及从 PyPI 安装的固定版本 Transformers 及其依赖。wheel 通过标准库下载并解包 —— **无需 pip，也无需系统 Python** —— 使用 `.installing` 锁文件和 `install.log`。源码运行时仅在直接下载 wheel 失败时才回退到 pip。
- 无网络、下载受阻或平台不兼容只会禁用表格识别 —— 其余转换仍然成功。
- `PDF2WORD_APP_DIR` 也会移动 `deps/` 目录。设置 `PDF2WORD_NO_AUTO_DEPS=1` 可关闭自动安装，改为手动安装。
- 备份或删除应用时请一并处理 `deps/`；删除后下次使用会重新触发下载。

### Python API

```python
from pdf2word import pdf_to_docx, ConversionConfig

# 简单转换
pdf_to_docx("scan.pdf", "output.docx")

# 自定义配置
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

# 高级用法
from pdf2word import PDF2WordConverter
converter = PDF2WordConverter(config)
converter.convert("scan.pdf", "output.docx")
```

## 项目结构

```
pdf2word/
├── pdf2word/              # Python 包
│   ├── __init__.py        # 导出接口
│   ├── ops.py             # Rust 绑定 + 纯 Python 回退
│   ├── pipeline.py        # 主转换流程
│   ├── cli.py             # 基于 Click 的命令行
│   ├── gui.py             # Tkinter 桌面 GUI
│   ├── app_paths.py       # 应用旁的配置/历史路径
│   ├── config_store.py    # JSON 配置持久化
│   ├── history.py         # SQLite 转换历史
│   ├── mcp_server.py      # MCP 工具定义（共享）
│   ├── mcp_http.py        # GUI / exe 的 HTTP MCP 服务
│   ├── table_deps.py      # 按需安装 torch/transformers
│   └── __main__.py        # CLI / GUI 入口
├── gui_main.py            # PyInstaller 入口脚本
├── pdf2word.spec          # PyInstaller Windows 构建配置
├── build_windows.bat      # 一键构建 Windows 应用
├── src/
│   └── lib.rs             # Rust 核心算子（PyO3）
├── tests/                 # Pytest 测试
├── .github/workflows/     # GitHub Actions CI/CD
├── Dockerfile             # 多阶段 Docker 构建
├── docker-compose.yml     # 开发/生产编排
├── pyproject.toml         # 构建配置（maturin + pytest）
├── Cargo.toml             # Rust crate 配置
└── README.md
```

### Rust 核心算子（位于 `src/lib.rs`）

| 函数 | 用途 | 相对 Python 的加速 |
|------|------|------------------|
| `coords_px_to_pt` | 批量像素→点坐标转换 | ~20-50× |
| `coords_pt_to_emu` | 点→EMU，用于精确的 OOXML 定位 | ~20-50× |
| `sort_table_cells` | 表格单元格按行优先排序 | ~10-20× |
| `sort_layout_blocks` | 阅读顺序的拓扑排序 | ~10-50× |
| `nms_boxes` | 检测框的非极大值抑制 | ~5-10× |
| `merge_text_lines` | 将文本行合并为段落 | ~5-10× |
| `compute_fit_font_size` | 估算适合边界框的字体大小 | ~10× |

### 数据流

```
PDF (fitz)
  → 按 DPI 渲染页面（pixmap）
  → 版面检测（Surya）
  → 阅读顺序（Surya OrderPredictor）
  → 对每个区块：
      ├─ 文本/标题/图注 → OCR（RapidOCR）
      ├─ 表格 → Table Transformer（结构）+ 单元格 OCR
      └─ 图片 → 高质量裁剪 + 图注关联
  → 按阅读顺序排序区块
  → 将图注与图片/表格关联
  → 按区域检测页眉/页脚
  → 生成 Word（python-docx）
      ├─ 带字体大小估算的段落
      ├─ 带合并单元格的原生 docx 表格
      ├─ 嵌入图片及其图注
      └─ 页眉/页脚处理
```

## 配置

```python
@dataclass
class ConversionConfig:
    dpi: int = 300                              # 渲染分辨率
    ocr_lang: str = "ch_sim+en"                 # OCR 语言（用 + 分隔，如 "ch_sim+en"）
    ocr_conf_threshold: float = 0.5             # OCR 最低置信度
    layout_model: str = "surya"                 # 版面检测器
    table_recognition: bool = True              # 启用表格处理
    table_model: str = "microsoft/table-transformer-structure-recognition"
    preserve_fonts: bool = True                 # 估算字体大小
    preserve_colors: bool = True                # 保留文本颜色
    merge_paragraphs: bool = True               # 将文本行合并为段落
    para_v_threshold_ratio: float = 0.5         # 段落合并阈值
    nms_iou_threshold: float = 0.45             # NMS IoU 阈值
    max_workers: int = 4                        # 并行页面工作线程数
    intra_threads: int = 0                      # 每个 OCR/表格模型的线程数；0 = 自动（CPU 核心数 // max_workers）
    inter_threads: int = 1                      # 各推理库的 inter-op 线程数
    extract_figures: bool = True                # 提取并嵌入图片/图像
    figure_min_size: int = 50                   # 图片最小尺寸（像素）
```

## Docker 使用

### 构建

```bash
# 生产镜像（多阶段构建，约 500MB）
docker build -t pdf2word .

# 开发镜像（挂载源码）
docker-compose up -d pdf2word-dev
docker exec -it pdf2word-dev bash
```

### 运行转换

```bash
# 单个文件
docker run --rm \
  -v $(pwd)/input:/app/input:ro \
  -v $(pwd)/output:/app/output \
  pdf2word convert /app/input/doc.pdf /app/output/doc.docx

# 批量
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

## 性能建议

1. **使用 GPU**：NVIDIA 显卡可安装 `onnxruntime-gpu` 替代 `onnxruntime`
2. **调整 DPI**：300 DPI 是最佳平衡点；小字用 400+，追求速度用 200
3. **页面并行**：多页 PDF 可设置 `max_workers`（4-8）
4. **Rust 扩展**：务必使用 `maturin develop --release` 构建，核心算子可获得 10-50× 加速
5. **表格模型**：Table Transformer 在 CPU 上运行；追求速度可考虑更小的模型
6. **避免 CPU 超订阅**：ONNX Runtime 和 PyTorch 默认都会占满所有 CPU 核心。4 个页面工作线程会变成 `4 × 核心数` 个线程争抢 CPU，导致 CPU 跑满甚至变慢。默认 `intra_threads=0` 会按 `CPU 核心数 // max_workers` 推导每个库的线程上限（例如 8 核 / 4 线程 → 每库 2 个线程）。可用 `--intra-threads N` 或 GUI 中的 `intra_threads` 覆盖。保持 `max_workers × intra_threads ≈ 核心数`。快速排查可用 `--workers 1`：此时每个库独占全部核心，但彼此不再争抢。
7. **保持 GUI 响应**：若转换导致界面卡顿，可降低 `max_workers` 或 `intra_threads`。

### 为什么 CPU 占用 100%

OCR（ONNX Runtime）、Table Transformer（PyTorch）和 MuPDF 页面渲染都是 CPU 密集型计算 —— 把图像数据留在内存中可以避免磁盘 I/O，但无法消除计算本身。设置 `OMP_NUM_THREADS`/`MKL_NUM_THREADS` 及各库的 ONNX 选项可以限制每个库可用的核心数；要完全把计算移出 CPU，需要 GPU 执行（CUDA 版 torch + `onnxruntime-gpu`）。

## CI/CD

GitHub Actions 工作流（`.github/workflows/ci.yml`）包含：
- **Lint**：Rust（fmt、clippy）+ Python（ruff、black）
- **测试**：Python 3.9-3.12 矩阵
- **构建 Wheel**：通过 maturin 构建 Linux manylinux2014
- **发布**：release 时发布到 PyPI
- **Docker**：多架构构建并推送到 Docker Hub
- **基准测试**：main 分支上的性能跟踪

## 路线图

- [x] 通过 Table Transformer 实现表格识别
- [x] 图片提取与嵌入
- [x] 图注关联（图片/表格）
- [x] 页眉/页脚检测
- [x] 并行页面处理
- [x] Docker 多阶段构建
- [x] GitHub Actions CI/CD
- [ ] 多栏版面保留（文本框架）
- [ ] 字体嵌入以实现完全一致的视觉还原
- [ ] Web API（FastAPI）部署
- [ ] 带回归检测的基准测试套件

## 许可证

MIT License —— 详见 LICENSE 文件。

## 赞助

![微信赞赏码](http://qiniu.yhbsoul.com/oneblog/qrcode/20200623133519785.jpg) | ![支付宝收款码](http://qiniu.yhbsoul.com/oneblog/qrcode/20200623133521901.jpg) |
