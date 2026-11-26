"""Tests for the HTTP MCP server lifecycle."""

import socket

from starlette.applications import Starlette

from pdf2word.mcp_http import MCPHTTPServer


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_http_app_starts_and_stops():
    server = MCPHTTPServer(port=_free_port())
    assert isinstance(server.create_app(), Starlette)

    error = server.start()
    assert error is None
    assert server.is_running

    server.stop()
    assert not server.is_running
