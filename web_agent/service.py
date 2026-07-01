from __future__ import annotations

from dataclasses import asdict
from typing import Any

from agents import ItemHelpers, RunConfig, Runner, RunState, set_default_openai_api
from agents.items import ToolApprovalItem
from agents.models.interface import Model
from agents.usage import serialize_usage

from .agent_factory import build_agent, connected_mcp_servers
from .config import WebAgentConfig
from .sessions import SerializedInterruption, WebAgentSessionStore


class WebAgentService:
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
            result = await Runner.run(
                agent,
                message,
                session=session,
                run_config=self._run_config(),
            )

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
            result = await Runner.run(
                agent,
                state,
                session=session,
                run_config=self._run_config(),
            )

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

    def _run_config(self) -> RunConfig:
        return RunConfig(tracing_disabled=self.config.resolved_tracing_disabled)


def create_web_agent_service(
    config: WebAgentConfig,
    *,
    model: Model | str | None = None,
) -> WebAgentService:
    set_default_openai_api(config.model_api)
    return WebAgentService(config, model=model)


def create_app(
    config: WebAgentConfig,
    *,
    model: Model | str | None = None,
) -> WebAgentService:
    return create_web_agent_service(config, model=model)


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
