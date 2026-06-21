from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from agents import (
    Agent,
    ModelSettings,
    ShellCallOutcome,
    ShellCommandOutput,
    ShellCommandRequest,
    ShellResult,
    ShellTool,
    ShellToolLocalSkill,
)
from agents.mcp import MCPServer, MCPServerSse, MCPServerStdio, MCPServerStreamableHttp
from agents.models.interface import Model

from .config import LocalSkillConfig, MCPServerConfig, WebAgentConfig


class WebAgentShellExecutor:
    def __init__(self, cwd: Path):
        self.cwd = cwd

    async def __call__(self, request: ShellCommandRequest) -> ShellResult:
        action = request.data.action
        outputs: list[ShellCommandOutput] = []
        for command in action.commands:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=self.cwd,
                env=os.environ.copy(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            timed_out = False
            try:
                timeout = (action.timeout_ms or 0) / 1000 or None
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                process.kill()
                stdout_bytes, stderr_bytes = await process.communicate()
                timed_out = True

            outputs.append(
                ShellCommandOutput(
                    command=command,
                    stdout=stdout_bytes.decode("utf-8", errors="ignore"),
                    stderr=stderr_bytes.decode("utf-8", errors="ignore"),
                    outcome=ShellCallOutcome(
                        type="timeout" if timed_out else "exit",
                        exit_code=process.returncode,
                    ),
                )
            )
            if timed_out:
                break

        return ShellResult(
            output=outputs,
            provider_data={"working_directory": str(self.cwd)},
        )


def build_agent(
    config: WebAgentConfig,
    *,
    mcp_servers: Sequence[MCPServer] | None = None,
    model: Model | str | None = None,
) -> Agent[Any]:
    tools: list[Any] = []
    if config.skills:
        tools.append(_build_shell_tool(config))

    resolved_model = model if model is not None else config.model
    return Agent(
        name=config.name,
        model=resolved_model,
        instructions=config.instructions,
        tools=tools,
        mcp_servers=list(mcp_servers or []),
        mcp_config={
            "convert_schemas_to_strict": config.convert_mcp_schemas_to_strict,
            "include_server_in_tool_names": config.include_server_in_tool_names,
        },
        model_settings=ModelSettings(),
    )


@asynccontextmanager
async def connected_mcp_servers(config: WebAgentConfig) -> AsyncIterator[list[MCPServer]]:
    servers = [_build_mcp_server(server_config) for server_config in config.mcp_servers]
    if not servers:
        yield []
        return

    connected: list[MCPServer] = []
    try:
        for server in servers:
            await server.connect()
            connected.append(server)
        yield connected
    finally:
        for server in reversed(connected):
            await server.cleanup()


def _build_shell_tool(config: WebAgentConfig) -> ShellTool:
    workdir = config.shell_workdir or Path.cwd()
    skills = [_to_shell_skill(skill) for skill in config.skills]
    return ShellTool(
        executor=WebAgentShellExecutor(workdir),
        needs_approval=config.shell_needs_approval,
        environment={
            "type": "local",
            "skills": skills,
        },
    )


def _to_shell_skill(skill: LocalSkillConfig) -> ShellToolLocalSkill:
    description = skill.description or f"Local skill loaded from {skill.path}"
    return {
        "name": skill.name,
        "description": description,
        "path": str(skill.path),
    }


def _build_mcp_server(config: MCPServerConfig) -> MCPServer:
    if config.transport == "stdio":
        if not config.command:
            raise ValueError(f"MCP stdio server {config.name!r} requires command")
        params: dict[str, Any] = {
            "command": config.command,
            "args": config.args,
        }
        if config.cwd is not None:
            params["cwd"] = str(config.cwd)
        return MCPServerStdio(
            name=config.name,
            params=cast(Any, params),
            cache_tools_list=config.cache_tools,
            require_approval=config.require_approval,
        )

    if config.transport == "sse":
        return MCPServerSse(
            name=config.name,
            params=cast(Any, _http_params(config)),
            cache_tools_list=config.cache_tools,
            require_approval=config.require_approval,
        )

    if config.transport == "streamable_http":
        return MCPServerStreamableHttp(
            name=config.name,
            params=cast(Any, _http_params(config)),
            cache_tools_list=config.cache_tools,
            require_approval=config.require_approval,
        )

    raise ValueError(f"Unsupported MCP transport: {config.transport}")


def _http_params(config: MCPServerConfig) -> dict[str, Any]:
    if not config.url:
        raise ValueError(f"MCP HTTP server {config.name!r} requires url")
    params: dict[str, Any] = {"url": config.url}
    if config.headers:
        params["headers"] = config.headers
    if config.timeout is not None:
        params["timeout"] = config.timeout
    return params
