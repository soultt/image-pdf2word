# MCP server design (2026-11-26, final)

## Scope

Expose PDF conversion over MCP from the Windows executable and GUI. The exe
starts the MCP HTTP server as soon as the GUI starts. There are no MCP tools
for configuration; API settings are maintained in `config.json` by the GUI,
CLI or by editing the file.

Validated decisions:

- No stdio entry point. The single tool set is served over streamable HTTP on
  `127.0.0.1` by a daemon thread inside the GUI process.
- Layout API usage is controlled by an explicit `use_layout_api` boolean;
  `layout_api_base` alone never enables the API and never decides the layout
  mode.
- `ConversionConfig` also carries `mcp_enabled` and `mcp_port`; the GUI has an
  "MCP 服务" tab with an enable switch, port, status and "应用并重启".
- The API switch is also exposed on the CLI as `--use-layout-api`, and in the
  GUI's "布局 API" tab. MCP tool parameters never include API settings.

## Modules

- `pdf2word/mcp_server.py`: shared `convert_pdf` tool and `convert_pdf_impl()`.
- `pdf2word/mcp_http.py`: `MCPHTTPServer` wrapping `streamable_http_app()` with
  uvicorn, `start()` / `stop()` and status/error reporting.
- `pdf2word/gui.py`: starts the server at launch, applies MCP settings with
  "应用并重启", stops it on close.

## Behavior

- `_process_page` uses `use_layout_api` to decide whether to run layout
  detection; when False the whole page is OCR'd as one text block.
- `_validate_config` requires `layout_api_base` and `layout_api_model` only
  when `use_layout_api` is True. The unreachable local Surya fallback was
  removed from `layout_predictor`.
- Invalid port (outside 1-65535) is rejected in the GUI. Port conflicts are
  reported in the status label and log without breaking the GUI.

## Testing

- Layout-mode tests use `use_layout_api`, including the case where a base URL
  is set but the flag is disabled.
- Config tests cover new defaults and GUI field coverage.
- MCP tests cover tool overrides, preservation of API config, HTTP app
  construction and start/stop lifecycle.
