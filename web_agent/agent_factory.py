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
    function_tool,
)
from agents.mcp import (
    MCPServer,
    MCPServerManager,
    MCPServerSse,
    MCPServerStdio,
    MCPServerStreamableHttp,
)
from agents.models.interface import Model
from agents.tool import FunctionTool

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
        tools.extend(_build_skill_tools(config))

    resolved_model = model if model is not None else config.model
    return Agent(
        name=config.name,
        model=resolved_model,
        instructions=_build_instructions(config),
        tools=tools,
        mcp_servers=list(mcp_servers or []),
        mcp_config={
            "convert_schemas_to_strict": config.convert_mcp_schemas_to_strict,
            "include_server_in_tool_names": config.include_server_in_tool_names,
        },
        model_settings=ModelSettings(),
    )


def _build_skill_tools(config: WebAgentConfig) -> list[ShellTool | FunctionTool]:
    if config.model_api == "chat_completions":
        return [_build_load_skill_tool(config.skills)]
    return [_build_shell_tool(config)]


@asynccontextmanager
async def connected_mcp_servers(config: WebAgentConfig) -> AsyncIterator[list[MCPServer]]:
    servers = [_build_mcp_server(server_config) for server_config in config.mcp_servers]
    async with MCPServerManager(
        servers,
        connect_timeout_seconds=config.mcp_connect_timeout_seconds,
        cleanup_timeout_seconds=config.mcp_cleanup_timeout_seconds,
        drop_failed_servers=True,
        strict=config.mcp_strict,
        connect_in_parallel=config.mcp_connect_in_parallel,
    ) as manager:
        yield manager.active_servers


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


def _build_load_skill_tool(skills: Sequence[LocalSkillConfig]) -> FunctionTool:
    skills_by_name = {skill.name: skill for skill in skills}
    aliases = {skill.name.replace("-", "_"): skill.name for skill in skills}
    available = ", ".join(sorted(skills_by_name)) or "none"

    def load_local_skill(skill_name: str) -> str:
        """Load the SKILL.md instructions for a configured local skill."""
        normalized_name = aliases.get(skill_name, skill_name)
        skill = skills_by_name.get(normalized_name)
        if skill is None:
            return f"Unknown local skill {skill_name!r}. Available skills: {available}."
        return _read_skill_text(skill)

    return function_tool(
        load_local_skill,
        name_override="load_local_skill",
        description_override=(
            "Load the SKILL.md instructions for one configured local skill. "
            f"Available skills: {available}."
        ),
    )


def _read_skill_text(skill: LocalSkillConfig) -> str:
    skill_file = skill.path / "SKILL.md" if skill.path.is_dir() else skill.path
    try:
        return skill_file.read_text(encoding="utf-8")
    except OSError:
        return skill.description or f"Local skill {skill.name}"


def _build_instructions(config: WebAgentConfig) -> str:
    if not config.skills:
        return config.instructions
    if config.model_api != "chat_completions":
        return config.instructions

    lines = [
        config.instructions.rstrip(),
        "",
        "Configured local skills are available through the load_local_skill tool.",
        "Call load_local_skill before applying a skill so you can follow its SKILL.md exactly.",
        "Available local skills:",
    ]
    for skill in config.skills:
        description = skill.description or f"Local skill loaded from {skill.path}"
        lines.append(f"- {skill.name}: {description}")
    return "\n".join(lines)


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
            require_approval=cast(Any, config.require_approval),
        )

    if config.transport == "sse":
        return MCPServerSse(
            name=config.name,
            params=cast(Any, _http_params(config)),
            cache_tools_list=config.cache_tools,
            require_approval=cast(Any, config.require_approval),
        )

    if config.transport == "streamable_http":
        return MCPServerStreamableHttp(
            name=config.name,
            params=cast(Any, _http_params(config)),
            cache_tools_list=config.cache_tools,
            require_approval=cast(Any, config.require_approval),
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
