from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from agents import ItemHelpers, Runner, RunState
from agents.items import ToolApprovalItem
from agents.models.interface import Model
from agents.usage import serialize_usage

from .agent_builder import build_agent, connected_mcp_servers
from .config import WebAgentConfig, load_config
from .sessions import SerializedInterruption, WebAgentSessionStore


class WebAgentApplication:
    def __init__(self, config: WebAgentConfig, *, model: Model | str | None = None):
        self.config = config
        self.model = model
        self.sessions = WebAgentSessionStore(config)

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        message = _require_str(payload.get("message"), "message")
        session_id = payload.get("session_id")
        metadata = self.sessions.get_or_create_metadata(
            str(session_id) if session_id else None,
            title=message,
        )
        session = await self.sessions.get_session(metadata.session_id)

        async with connected_mcp_servers(self.config) as mcp_servers:
            agent = build_agent(self.config, mcp_servers=mcp_servers, model=self.model)
            result = await Runner.run(agent, message, session=session)

        metadata = self.sessions.touch_session(metadata.session_id, title=message)
        if result.interruptions:
            state = result.to_state()
            serialized_interruptions = [
                _serialize_interruption(item) for item in result.interruptions
            ]
            self.sessions.save_pending_state(
                metadata.session_id,
                state_json=state.to_json(),
                interruptions=serialized_interruptions,
            )
            return {
                "session": asdict(metadata),
                "status": "needs_approval",
                "interruptions": serialized_interruptions,
                "output": None,
            }

        self.sessions.clear_pending_state(metadata.session_id)
        return {
            "session": asdict(metadata),
            "status": "completed",
            "output": _result_output(result),
            "interruptions": [],
            "usage": serialize_usage(result.context_wrapper.usage),
        }

    async def approve(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        approve = bool(payload.get("approve", True))
        rejection_message = payload.get("rejection_message")
        pending = self.sessions.load_pending_state(session_id)
        if pending is None:
            return {"status": "not_found", "message": "No pending approval for this session."}

        async with connected_mcp_servers(self.config) as mcp_servers:
            agent = build_agent(self.config, mcp_servers=mcp_servers, model=self.model)
            state_payload = pending.get("state")
            if not isinstance(state_payload, dict):
                raise ValueError("Stored pending state is invalid.")
            state = await RunState.from_json(agent, state_payload)
            interruptions = state.get_interruptions()
            for item in interruptions:
                if approve:
                    state.approve(item)
                else:
                    state.reject(
                        item,
                        rejection_message=str(rejection_message)
                        if rejection_message is not None
                        else None,
                    )
            session = await self.sessions.get_session(session_id)
            result = await Runner.run(agent, state, session=session)

        metadata = self.sessions.touch_session(session_id)
        if result.interruptions:
            state = result.to_state()
            serialized_interruptions = [
                _serialize_interruption(item) for item in result.interruptions
            ]
            self.sessions.save_pending_state(
                session_id,
                state_json=state.to_json(),
                interruptions=serialized_interruptions,
            )
            return {
                "session": asdict(metadata),
                "status": "needs_approval",
                "interruptions": serialized_interruptions,
                "output": None,
            }

        self.sessions.clear_pending_state(session_id)
        return {
            "session": asdict(metadata),
            "status": "completed",
            "output": _result_output(result),
            "interruptions": [],
            "usage": serialize_usage(result.context_wrapper.usage),
        }

    async def list_sessions(self) -> dict[str, Any]:
        return {"sessions": [asdict(item) for item in self.sessions.list_sessions()]}

    async def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = payload.get("title")
        metadata = self.sessions.create_session(title=str(title) if title else None)
        return {"session": asdict(metadata)}

    async def clear_session(self, session_id: str) -> dict[str, Any]:
        removed = await self.sessions.clear_session(session_id)
        return {"removed": removed, "session_id": session_id}

    async def compact_session(self, session_id: str) -> dict[str, Any]:
        if not self.config.compaction.enabled:
            return {
                "status": "disabled",
                "message": "Compaction is disabled in this web agent configuration.",
            }
        compacted = await self.sessions.compact_session(session_id, force=True)
        if not compacted:
            return {"status": "disabled", "message": "This session does not support compaction."}
        return {"status": "completed", "session_id": session_id}

    async def session_items(self, session_id: str, limit: int | None = 50) -> dict[str, Any]:
        items = await self.sessions.get_items(session_id, limit=limit)
        return {"session_id": session_id, "items": items}


def create_app(
    config: WebAgentConfig,
    *,
    model: Model | str | None = None,
) -> WebAgentApplication:
    return WebAgentApplication(config, model=model)


class WebAgentRequestHandler(BaseHTTPRequestHandler):
    app: WebAgentApplication
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
    app = WebAgentApplication(config, model=model)

    class Handler(WebAgentRequestHandler):
        """Request handler bound to one application instance."""

    Handler.app = app
    return ThreadingHTTPServer((host, port), Handler)


def run_server(host: str, port: int, config: WebAgentConfig) -> None:
    server = create_server(host, port, config)
    print(f"Web agent ready at http://{host}:{port}")
    print(f"Model: {config.model}")
    print(f"Session DB: {config.resolved_sessions_db}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping web agent.")
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the local Agents SDK web agent.")
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


def _serialize_interruption(item: ToolApprovalItem) -> SerializedInterruption:
    return {
        "tool_name": item.name,
        "qualified_name": item.qualified_name,
        "call_id": item.call_id,
        "arguments": item.arguments,
    }


def _result_output(result: Any) -> str:
    output = result.final_output or ItemHelpers.text_message_outputs(result.new_items)
    return str(output)


def _require_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")
    return value
