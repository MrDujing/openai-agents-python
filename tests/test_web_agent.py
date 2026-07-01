from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from agents import Agent
from agents.mcp import MCPServerStdio
from agents.tool import FunctionTool
from agents.tool_context import ToolContext
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message
from web_agent import (
    CompactionConfig,
    LocalSkillConfig,
    WebAgentConfig,
    create_app,
    load_config,
)
from web_agent.agent_factory import build_agent, connected_mcp_servers
from web_agent.app import WebAgentRequestHandler, create_server
from web_agent.sessions import WebAgentSessionStore


def test_load_config_parses_skills_and_mcp(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    config_path = tmp_path / "web-agent.json"
    config_path.write_text(
        json.dumps(
            {
                "name": "Ops Agent",
                "model": "gpt-test",
                "model_api": "chat_completions",
                "tracing_disabled": False,
                "data_dir": "agent-data",
                "mcp_connect_timeout_seconds": 2.5,
                "mcp_cleanup_timeout_seconds": 1.5,
                "mcp_strict": True,
                "mcp_connect_in_parallel": True,
                "compaction": {
                    "enabled": False,
                    "auto": False,
                    "candidate_threshold": 3,
                },
                "skills": [
                    {
                        "name": "local-skill",
                        "path": str(skill_dir),
                        "description": "Local test skill",
                    }
                ],
                "mcp_servers": [
                    {
                        "name": "fs",
                        "transport": "stdio",
                        "command": "python",
                        "args": ["server.py"],
                        "require_approval": {
                            "always": {"tool_names": ["write_file"]},
                            "never": {"tool_names": ["read_file"]},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path, env={})

    assert config.name == "Ops Agent"
    assert config.model == "gpt-test"
    assert config.model_api == "chat_completions"
    assert config.tracing_disabled is False
    assert config.resolved_tracing_disabled is False
    assert config.data_dir == tmp_path / "agent-data"
    assert config.compaction.enabled is False
    assert config.compaction.auto is False
    assert config.compaction.candidate_threshold == 3
    assert config.mcp_connect_timeout_seconds == 2.5
    assert config.mcp_cleanup_timeout_seconds == 1.5
    assert config.mcp_strict is True
    assert config.mcp_connect_in_parallel is True
    assert config.skills[0].name == "local-skill"
    assert config.skills[0].path == skill_dir
    assert config.mcp_servers[0].name == "fs"
    assert config.mcp_servers[0].require_approval == {
        "always": {"tool_names": ["write_file"]},
        "never": {"tool_names": ["read_file"]},
    }


def test_demo_config_loads_relative_skill_and_mcp_paths() -> None:
    demo_config_path = Path("web_agent/demo/web-agent-demo.json")

    config = load_config(demo_config_path, env={})

    assert config.skills[0].name == "briefing-writer"
    assert config.model_api == "chat_completions"
    assert config.resolved_tracing_disabled is True
    assert config.data_dir == Path(".web-agent-data/demo").resolve()
    expected_skill_path = (demo_config_path.parent / "skills" / "briefing-writer").resolve()
    assert config.skills[0].path == expected_skill_path
    assert config.mcp_servers[0].name == "demo-policy"
    assert config.mcp_servers[0].command == sys.executable
    assert config.mcp_servers[0].args == ["mcp_server.py"]
    assert config.mcp_servers[0].cwd == demo_config_path.parent.resolve()


def test_env_overrides_model_api_model_and_tracing(tmp_path: Path) -> None:
    config = load_config(
        None,
        env={
            "OPENAI_DEFAULT_MODEL": "provider-default",
            "WEB_AGENT_MODEL_API": "chat_completions",
            "WEB_AGENT_TRACING_DISABLED": "false",
            "WEB_AGENT_DATA_DIR": str(tmp_path),
            "WEB_AGENT_MCP_STRICT": "true",
            "WEB_AGENT_MCP_CONNECT_IN_PARALLEL": "true",
            "WEB_AGENT_MCP_CONNECT_TIMEOUT_SECONDS": "3",
            "WEB_AGENT_MCP_CLEANUP_TIMEOUT_SECONDS": "4",
        },
    )

    assert config.model == "provider-default"
    assert config.model_api == "chat_completions"
    assert config.resolved_tracing_disabled is False
    assert config.mcp_strict is True
    assert config.mcp_connect_in_parallel is True
    assert config.mcp_connect_timeout_seconds == 3
    assert config.mcp_cleanup_timeout_seconds == 4


@pytest.mark.asyncio
async def test_connected_mcp_servers_uses_sdk_manager(tmp_path: Path) -> None:
    config = WebAgentConfig(
        data_dir=tmp_path,
        compaction=_disabled_compaction(),
        mcp_connect_timeout_seconds=2,
        mcp_cleanup_timeout_seconds=3,
        mcp_strict=True,
        mcp_connect_in_parallel=True,
    )

    class FakeManager:
        def __init__(self, servers: list[object], **kwargs: object) -> None:
            self.active_servers = servers
            self.kwargs = kwargs
            manager_calls.append(self)

        async def __aenter__(self) -> FakeManager:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    manager_calls: list[FakeManager] = []
    with patch("web_agent.agent_factory.MCPServerManager", FakeManager):
        async with connected_mcp_servers(config) as servers:
            assert servers == []

    assert manager_calls[0].kwargs == {
        "connect_timeout_seconds": 2,
        "cleanup_timeout_seconds": 3,
        "drop_failed_servers": True,
        "strict": True,
        "connect_in_parallel": True,
    }


@pytest.mark.asyncio
async def test_demo_mcp_server_lists_and_calls_tools() -> None:
    demo_config_path = Path("web_agent/demo/web-agent-demo.json")
    config = load_config(demo_config_path, env={})

    async with connected_mcp_servers(config) as servers:
        [server] = servers
        assert isinstance(server, MCPServerStdio)
        tools = await server.list_tools()
        tool_names = {tool.name for tool in tools}
        assert "lookup_demo_policy" in tool_names

        result = await server.call_tool("lookup_demo_policy", {"topic": "session"})

    text_parts: list[str] = []
    for item in result.content:
        if getattr(item, "type", None) != "text":
            continue
        item_text = getattr(item, "text", None)
        if isinstance(item_text, str):
            text_parts.append(item_text)
    text = "\n".join(text_parts)
    assert "one session per task" in text


@pytest.mark.asyncio
async def test_session_store_persists_metadata(tmp_path: Path) -> None:
    config = WebAgentConfig(data_dir=tmp_path, compaction=_disabled_compaction())
    store = WebAgentSessionStore(config)

    first = store.create_session("First chat")
    second_store = WebAgentSessionStore(config)

    assert second_store.list_sessions()[0].session_id == first.session_id

    removed = await second_store.clear_session(first.session_id)
    assert removed is True
    assert second_store.list_sessions() == []


def test_build_agent_adds_shell_tool_for_configured_skill(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    config = WebAgentConfig(data_dir=tmp_path, compaction=_disabled_compaction())
    config.skills.append(_skill_config("csv-workbench", skill_dir))

    agent = build_agent(config, model=FakeModel())

    assert isinstance(agent, Agent)
    assert len(agent.tools) == 1
    assert agent.tools[0].name == "shell"


@pytest.mark.asyncio
async def test_build_agent_adds_generic_skill_loader_for_chat_completions(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "briefing-writer"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Briefing Writer\n", encoding="utf-8")
    config = WebAgentConfig(
        data_dir=tmp_path,
        model_api="chat_completions",
        compaction=_disabled_compaction(),
    )
    config.skills.append(_skill_config("briefing-writer", skill_dir))

    agent = build_agent(config, model=FakeModel())

    assert len(agent.tools) == 1
    tool = agent.tools[0]
    assert isinstance(tool, FunctionTool)
    assert tool.name == "load_local_skill"
    assert isinstance(agent.instructions, str)
    assert "load_local_skill" in agent.instructions

    output = await tool.on_invoke_tool(
        _tool_context("load_local_skill"),
        json.dumps({"skill_name": "briefing-writer"}),
    )
    assert output == "# Briefing Writer\n"


@pytest.mark.asyncio
async def test_web_agent_chat_uses_session_and_fake_model(tmp_path: Path) -> None:
    config = WebAgentConfig(data_dir=tmp_path, compaction=_disabled_compaction())
    model = FakeModel(initial_output=[get_text_message("hello from fake model")])
    app = create_app(config, model=model)

    response = await app.chat({"message": "hello"})

    assert response["status"] == "completed"
    assert response["output"] == "hello from fake model"
    assert response["session"]["session_id"]
    assert len(await app.sessions.get_items(response["session"]["session_id"])) == 2


def test_web_agent_run_config_uses_resolved_tracing(tmp_path: Path) -> None:
    config = WebAgentConfig(
        data_dir=tmp_path,
        model_api="chat_completions",
        compaction=_disabled_compaction(),
    )
    app = create_app(config, model=FakeModel())

    assert app._run_config().tracing_disabled is True

    config.tracing_disabled = False
    assert app._run_config().tracing_disabled is False


def test_web_server_uses_top_level_static_dir(tmp_path: Path) -> None:
    config = WebAgentConfig(data_dir=tmp_path, compaction=_disabled_compaction())
    server = create_server("127.0.0.1", 0, config, model=FakeModel())
    try:
        handler = cast(type[WebAgentRequestHandler], server.RequestHandlerClass)
        assert handler.static_dir == Path("web_agent/static").resolve()
    finally:
        server.server_close()


@pytest.mark.asyncio
async def test_manual_compaction_reports_disabled(tmp_path: Path) -> None:
    config = WebAgentConfig(data_dir=tmp_path, compaction=_disabled_compaction())
    app = create_app(config, model=FakeModel())
    session = app.sessions.create_session()

    response = await app.compact_session(session.session_id)

    assert response["status"] == "disabled"


def _disabled_compaction() -> CompactionConfig:
    return CompactionConfig(enabled=False, auto=False)


def _skill_config(name: str, path: Path) -> LocalSkillConfig:
    return LocalSkillConfig(name=name, path=path, description="Test skill")


def _tool_context(tool_name: str) -> ToolContext[None]:
    return ToolContext(None, tool_name=tool_name, tool_call_id="call_1", tool_arguments="")
