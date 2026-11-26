"""HTTP MCP server that runs alongside the GUI / packaged Windows app."""

from __future__ import annotations

import socket
import threading
import time
import traceback
from typing import Optional

import uvicorn
from starlette.applications import Starlette

from .mcp_server import mcp


def is_port_bindable(host: str = "127.0.0.1", port: int = 8765) -> bool:
    """Check whether a plain TCP socket can bind to ``host:port``.

    Windows can reject a port with ``WinError 10013`` (reserved by a system
    service, security software or a hypervisor) even when nothing is listening
    on it. Probing first avoids starting uvicorn only to have it exit with a
    confusing SystemExit.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def find_available_port(host: str = "127.0.0.1", preferred: int = 8765) -> int:
    """Return a bindable port, trying the documented default first."""
    for port in (preferred, 8765, 8766, 8888, 8000, 8080):
        if is_port_bindable(host, port):
            return port
    # Last resort: let the OS assign a free ephemeral port.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


class MCPHTTPServer:
    """Runs the shared MCP tool set over streamable HTTP on localhost."""

    def __init__(self, port: int = 8765, host: str = "127.0.0.1", path: str = "/mcp"):
        self.port = int(port)
        self.host = host
        self.path = path
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None
        self._error: Optional[str] = None

    def create_app(self) -> Starlette:
        """Build the Starlette app from the shared MCP server instance."""
        # SDK 2.x uses streamable_http_path; 1.x used path.
        try:
            return mcp.streamable_http_app(
                streamable_http_path=self.path, host=self.host
            )
        except TypeError:
            try:
                return mcp.streamable_http_app(streamable_http_path=self.path)
            except TypeError:
                return mcp.streamable_http_app(path=self.path)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def error(self) -> Optional[str]:
        return self._error

    def _run(self, server: uvicorn.Server) -> None:
        try:
            server.run()
        except BaseException as exc:  # uvicorn may raise SystemExit on bind error
            self._error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"

    def start(self) -> Optional[str]:
        """Start the HTTP server; returns an error message or None on success."""
        if self.is_running:
            return None
        self._error = None
        app = self.create_app()
        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
            # uvicorn's default dictConfig cannot resolve its formatter class
            # in a PyInstaller bundle ("Unable to configure formatter
            # 'default'"), so skip log config entirely.
            log_config=None,
        )
        server = uvicorn.Server(config)
        self._server = server
        self._thread = threading.Thread(
            target=self._run, args=(server,), daemon=True, name="pdf2word-mcp-http"
        )
        self._thread.start()

        for _ in range(100):
            if server.started:
                return None
            if not self._thread.is_alive():
                return self._error or f"MCP HTTP server exited on port {self.port}"
            time.sleep(0.02)
        return "MCP HTTP server did not become ready in time"

    def stop(self) -> None:
        """Request shutdown and wait briefly for the server thread to exit."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._server = None
        self._thread = None
