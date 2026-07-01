from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from agents.models.interface import Model

from .config import WebAgentConfig, load_config
from .service import WebAgentService, create_web_agent_service


class WebAgentRequestHandler(BaseHTTPRequestHandler):
    app: WebAgentService
    static_dir = Path(__file__).resolve().parent / "static"

    def do_GET(self) -> None:
        self._handle_request("GET")

    def do_POST(self) -> None:
        self._handle_request("POST")

    def do_DELETE(self) -> None:
        self._handle_request("DELETE")

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _handle_request(self, method: str) -> None:
        try:
            response = asyncio.run(self._dispatch(method))
        except Exception as exc:
            self._write_json(
                {"error": exc.__class__.__name__, "message": str(exc)},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        if response is None:
            return
        status, payload = response
        self._write_json(payload, status=status)

    async def _dispatch(self, method: str) -> tuple[HTTPStatus, dict[str, Any]] | None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if method == "GET" and path == "/api/sessions":
            return HTTPStatus.OK, await self.app.list_sessions()
        if method == "POST" and path == "/api/sessions":
            return HTTPStatus.CREATED, await self.app.create_session(self._read_json())
        if method == "POST" and path == "/api/chat":
            return HTTPStatus.OK, await self.app.chat(self._read_json())

        session_route = _match_session_route(path)
        if session_route is not None:
            session_id, action = session_route
            if method == "DELETE" and action is None:
                return HTTPStatus.OK, await self.app.clear_session(session_id)
            if method == "POST" and action == "compact":
                return HTTPStatus.OK, await self.app.compact_session(session_id)
            if method == "POST" and action == "approve":
                return HTTPStatus.OK, await self.app.approve(session_id, self._read_json())
            if method == "GET" and action == "items":
                return HTTPStatus.OK, await self.app.session_items(session_id)

        if method == "GET":
            self._serve_static(path)
            return None

        return HTTPStatus.NOT_FOUND, {"error": "not_found", "message": "Route not found."}

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    def _serve_static(self, path: str) -> None:
        if path in {"", "/"}:
            path = "/index.html"
        candidate = (self.static_dir / path.lstrip("/")).resolve()
        static_root = self.static_dir.resolve()
        if static_root not in candidate.parents and candidate != static_root:
            self._write_json(
                {"error": "not_found", "message": "Route not found."},
                status=HTTPStatus.NOT_FOUND,
            )
            return
        if not candidate.exists() or not candidate.is_file():
            self._write_json(
                {"error": "not_found", "message": "Route not found."},
                status=HTTPStatus.NOT_FOUND,
            )
            return

        content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        data = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _write_json(self, payload: dict[str, Any], *, status: HTTPStatus) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def create_server(
    host: str,
    port: int,
    config: WebAgentConfig,
    *,
    model: Model | str | None = None,
) -> ThreadingHTTPServer:
    app = create_web_agent_service(config, model=model)

    class Handler(WebAgentRequestHandler):
        """Request handler bound to one application instance."""

    Handler.app = app
    return ThreadingHTTPServer((host, port), Handler)


def run_server(host: str, port: int, config: WebAgentConfig) -> None:
    server = create_server(host, port, config)
    print(f"Web agent ready at http://{host}:{port}")
    print(f"Model: {config.model}")
    print(f"Model API: {config.model_api}")
    print(f"Tracing disabled: {config.resolved_tracing_disabled}")
    print(f"Session DB: {config.resolved_sessions_db}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping web agent.")
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Agents SDK Web Agent app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8008)
    parser.add_argument("--config", default=None, help="Path to a JSON web-agent config file.")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    run_server(args.host, args.port, config)


def _match_session_route(path: str) -> tuple[str, str | None] | None:
    parts = [part for part in path.split("/") if part]
    if len(parts) < 3 or parts[0] != "api" or parts[1] != "sessions":
        return None
    if len(parts) == 3:
        return parts[2], None
    if len(parts) == 4:
        return parts[2], parts[3]
    return None
