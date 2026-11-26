# Windows desktop GUI design (2026-11-26)

## Scope

Add a Tkinter desktop application for Windows on top of the existing CLI and
pipeline. No CLI behavior changes. Three new concerns:

1. Parameter configuration: every `ConversionConfig` field is editable in the
   GUI and persisted to `config.json`.
2. Conversion history: every conversion is recorded in `history.db` (SQLite)
   with parameters, status, duration and error.
3. Packaging: PyInstaller single-file `pdf2word.exe`.

Validated decisions:

- GUI framework: Tkinter (stdlib, no new runtime dependency).
- Config: all `ConversionConfig` fields, persisted as JSON.
- History: SQLite, searchable, with open-output and retry actions.
- Config and history live **next to the executable** (`sys.executable` parent
  when frozen, project root when running from source), not in `%APPDATA%`.
- Deliver source plus `pdf2word.spec` and `build_windows.bat`.

## Modules

- `pdf2word/app_paths.py`: resolves the application directory and the paths of
  `config.json` / `history.db`. `PDF2WORD_APP_DIR` overrides it for tests.
- `pdf2word/config_store.py`: `ConversionConfig` <-> JSON dict, load with
  fallback to defaults on corrupt input, save, reset.
- `pdf2word/history.py`: `HistoryDB` with `add_conversion`,
  `finish_conversion`, `list_conversions`, `search`, `get`, `delete`, `clear`;
  `MemoryHistory` fallback when SQLite is unavailable.
- `pdf2word/gui.py`: Tkinter application.

## Data flow

1. GUI loads saved config into Tk variables on startup.
2. User selects one or more PDFs and an output directory, adjusts parameters,
   clicks "开始转换".
3. GUI validates configuration (`layout_api_base` requires
   `layout_api_model`), saves the config, starts a worker thread.
4. For each file, the worker inserts a `running` history row, converts via a
   shared `PDF2WordConverter`, then updates status/duration/error.
5. Progress and log events go through a `queue.Queue`; the UI drains it with
   `Tk.after()` so no worker thread touches Tk widgets.
6. History tab lists, filters, opens output (`os.startfile`) and retries using
   the parameter snapshot stored on the row.

## Error handling

- Corrupt config -> defaults. SQLite failure -> in-memory history plus a log
  message. Per-file conversion errors are recorded and do not stop the batch.
- Successful, failed and running rows are all persisted.

## Testing and packaging

- Unit tests for config round-trip / fallback, history CRUD / search, and
  coverage of all config fields by the GUI parameter map. GUI itself is not
  instantiated in headless tests.
- `gui_main.py` + `pdf2word.spec` + `build_windows.bat` produce
  `dist/pdf2word.exe`. Heavy lazy dependencies (ONNX, Surya, torch,
  transformers, Rust extension) are collected into the bundle.
