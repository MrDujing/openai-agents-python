from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

MCPTransport = Literal["stdio", "sse", "streamable_http"]
MCPRequireApproval = bool | Literal["always", "never"]
ModelAPI = Literal["responses", "chat_completions"]


def _default_data_dir() -> Path:
    return Path(__file__).resolve().parent / ".data"


@dataclass
class LocalSkillConfig:
    name: str
    path: Path
    description: str = ""


@dataclass
class MCPServerConfig:
    name: str
    transport: MCPTransport
    command: str | None = None
    args: list[str] = field(default_factory=list)
    cwd: Path | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float | None = None
    cache_tools: bool = True
    require_approval: MCPRequireApproval = False


@dataclass
class CompactionConfig:
    enabled: bool = True
    model: str = "gpt-4.1"
    candidate_threshold: int = 10
    mode: Literal["previous_response_id", "input", "auto"] = "auto"
    auto: bool = True


@dataclass
class WebAgentConfig:
    name: str = "Local Web Agent"
    model: str = "gpt-5.4-mini"
    model_api: ModelAPI = "responses"
    tracing_disabled: bool | None = None
    instructions: str = (
        "You are a helpful local web agent. Answer clearly and ask for clarification "
        "when the user request is ambiguous."
    )
    data_dir: Path = field(default_factory=_default_data_dir)
    sessions_db: Path | None = None
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
    skills: list[LocalSkillConfig] = field(default_factory=list)
    mcp_servers: list[MCPServerConfig] = field(default_factory=list)
    shell_workdir: Path | None = None
    shell_needs_approval: bool = True
    include_server_in_tool_names: bool = True
    convert_mcp_schemas_to_strict: bool = True

    @property
    def resolved_sessions_db(self) -> Path:
        return self.sessions_db or self.data_dir / "sessions.sqlite"

    @property
    def resolved_tracing_disabled(self) -> bool:
        if self.tracing_disabled is not None:
            return self.tracing_disabled
        return self.model_api == "chat_completions"


def load_config(
    config_path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> WebAgentConfig:
    source_env = os.environ if env is None else env
    raw_config: dict[str, Any] = {}

    config_path_value = config_path or source_env.get("WEB_AGENT_CONFIG")
    if config_path_value:
        path = Path(config_path_value).expanduser()
        raw_config = _read_json_object(path)

    config = WebAgentConfig()
    _apply_mapping(config, raw_config)
    _apply_env(config, source_env)
    base_dir = Path(config_path_value).expanduser().parent if config_path_value else Path.cwd()
    _normalize_paths(config, base_dir=base_dir)
    return config


def _read_json_object(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return payload


def _apply_mapping(config: WebAgentConfig, payload: dict[str, Any]) -> None:
    if "name" in payload:
        config.name = _require_str(payload["name"], "name")
    if "model" in payload:
        config.model = _require_str(payload["model"], "model")
    if "model_api" in payload:
        config.model_api = _parse_model_api(payload["model_api"], "model_api")
    if "tracing_disabled" in payload:
        config.tracing_disabled = bool(payload["tracing_disabled"])
    if "instructions" in payload:
        config.instructions = _require_str(payload["instructions"], "instructions")

    if "data_dir" in payload:
        config.data_dir = Path(_require_str(payload["data_dir"], "data_dir"))
    if "sessions_db" in payload:
        config.sessions_db = Path(_require_str(payload["sessions_db"], "sessions_db"))
    if "shell_workdir" in payload:
        config.shell_workdir = Path(_require_str(payload["shell_workdir"], "shell_workdir"))
    if "shell_needs_approval" in payload:
        config.shell_needs_approval = bool(payload["shell_needs_approval"])
    if "include_server_in_tool_names" in payload:
        config.include_server_in_tool_names = bool(payload["include_server_in_tool_names"])
    if "convert_mcp_schemas_to_strict" in payload:
        config.convert_mcp_schemas_to_strict = bool(payload["convert_mcp_schemas_to_strict"])

    if isinstance(payload.get("compaction"), dict):
        _apply_compaction(config.compaction, payload["compaction"])

    if "skills" in payload:
        config.skills = _parse_skills(payload["skills"])
    if "mcp_servers" in payload:
        config.mcp_servers = _parse_mcp_servers(payload["mcp_servers"])


def _apply_compaction(config: CompactionConfig, payload: dict[str, Any]) -> None:
    if "enabled" in payload:
        config.enabled = bool(payload["enabled"])
    if "model" in payload:
        config.model = _require_str(payload["model"], "compaction.model")
    if "candidate_threshold" in payload:
        config.candidate_threshold = _positive_int(
            payload["candidate_threshold"], "compaction.candidate_threshold"
        )
    if "mode" in payload:
        config.mode = _parse_compaction_mode(payload["mode"], "compaction.mode")
    if "auto" in payload:
        config.auto = bool(payload["auto"])


def _parse_skills(payload: Any) -> list[LocalSkillConfig]:
    if not isinstance(payload, list):
        raise ValueError("skills must be a list")
    return [_parse_skill(item, index) for index, item in enumerate(payload)]


def _parse_skill(payload: Any, index: int) -> LocalSkillConfig:
    if not isinstance(payload, dict):
        raise ValueError(f"skills[{index}] must be an object")
    return LocalSkillConfig(
        name=_require_str(payload.get("name"), f"skills[{index}].name"),
        description=_require_str(payload.get("description"), f"skills[{index}].description")
        if payload.get("description") is not None
        else "",
        path=Path(_require_str(payload.get("path"), f"skills[{index}].path")),
    )


def _parse_mcp_servers(payload: Any) -> list[MCPServerConfig]:
    if not isinstance(payload, list):
        raise ValueError("mcp_servers must be a list")
    return [_parse_mcp_server(item, index) for index, item in enumerate(payload)]


def _parse_mcp_server(payload: Any, index: int) -> MCPServerConfig:
    if not isinstance(payload, dict):
        raise ValueError(f"mcp_servers[{index}] must be an object")

    transport = _parse_mcp_transport(
        payload.get("transport"),
        f"mcp_servers[{index}].transport",
    )
    if transport not in {"stdio", "sse", "streamable_http"}:
        raise ValueError(f"Unsupported MCP transport: {transport}")

    args = payload.get("args", [])
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ValueError(f"mcp_servers[{index}].args must be a list of strings")

    headers = payload.get("headers", {})
    if not isinstance(headers, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
    ):
        raise ValueError(f"mcp_servers[{index}].headers must be a string map")

    require_approval = _parse_require_approval(
        payload.get("require_approval", False),
        f"mcp_servers[{index}].require_approval",
    )

    timeout = payload.get("timeout")
    if timeout is not None and not isinstance(timeout, int | float):
        raise ValueError(f"mcp_servers[{index}].timeout must be numeric")

    return MCPServerConfig(
        name=_require_str(payload.get("name"), f"mcp_servers[{index}].name"),
        transport=transport,
        command=_optional_config_str(payload.get("command"), f"mcp_servers[{index}].command"),
        args=list(args),
        cwd=Path(payload["cwd"]) if "cwd" in payload else None,
        url=_optional_config_str(payload.get("url"), f"mcp_servers[{index}].url"),
        headers=dict(headers),
        timeout=float(timeout) if timeout is not None else None,
        cache_tools=bool(payload.get("cache_tools", True)),
        require_approval=require_approval,
    )


def _parse_require_approval(value: Any, name: str) -> MCPRequireApproval:
    if isinstance(value, bool):
        return value
    if value == "always":
        return "always"
    if value == "never":
        return "never"
    raise ValueError(f"{name} must be true, false, always, or never")


def _parse_compaction_mode(
    value: Any,
    name: str,
) -> Literal["previous_response_id", "input", "auto"]:
    mode = _require_str(value, name)
    if mode == "previous_response_id":
        return "previous_response_id"
    if mode == "input":
        return "input"
    if mode == "auto":
        return "auto"
    raise ValueError(f"{name} must be previous_response_id, input, or auto")


def _parse_mcp_transport(value: Any, name: str) -> MCPTransport:
    transport = _require_str(value, name)
    if transport == "stdio":
        return "stdio"
    if transport == "sse":
        return "sse"
    if transport == "streamable_http":
        return "streamable_http"
    raise ValueError(f"Unsupported MCP transport: {transport}")


def _parse_model_api(value: Any, name: str) -> ModelAPI:
    model_api = _require_str(value, name)
    if model_api == "responses":
        return "responses"
    if model_api == "chat_completions":
        return "chat_completions"
    raise ValueError(f"{name} must be responses or chat_completions")


def _optional_config_str(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _require_str(value, name)


def _apply_env(config: WebAgentConfig, env: Mapping[str, str]) -> None:
    if env.get("WEB_AGENT_MODEL"):
        config.model = env["WEB_AGENT_MODEL"]
    elif env.get("OPENAI_DEFAULT_MODEL"):
        config.model = env["OPENAI_DEFAULT_MODEL"]
    if env.get("WEB_AGENT_NAME"):
        config.name = env["WEB_AGENT_NAME"]
    if env.get("WEB_AGENT_MODEL_API"):
        config.model_api = _parse_model_api(env["WEB_AGENT_MODEL_API"], "WEB_AGENT_MODEL_API")
    if env.get("WEB_AGENT_TRACING_DISABLED"):
        config.tracing_disabled = _env_bool(env["WEB_AGENT_TRACING_DISABLED"])
    if env.get("WEB_AGENT_INSTRUCTIONS"):
        config.instructions = env["WEB_AGENT_INSTRUCTIONS"]
    if env.get("WEB_AGENT_DATA_DIR"):
        config.data_dir = Path(env["WEB_AGENT_DATA_DIR"])
    if env.get("WEB_AGENT_SESSIONS_DB"):
        config.sessions_db = Path(env["WEB_AGENT_SESSIONS_DB"])
    if env.get("WEB_AGENT_COMPACTION_ENABLED"):
        config.compaction.enabled = _env_bool(env["WEB_AGENT_COMPACTION_ENABLED"])
    if env.get("WEB_AGENT_COMPACTION_AUTO"):
        config.compaction.auto = _env_bool(env["WEB_AGENT_COMPACTION_AUTO"])
    if env.get("WEB_AGENT_COMPACTION_MODEL"):
        config.compaction.model = env["WEB_AGENT_COMPACTION_MODEL"]
    if env.get("WEB_AGENT_COMPACTION_THRESHOLD"):
        config.compaction.candidate_threshold = _positive_int(
            env["WEB_AGENT_COMPACTION_THRESHOLD"], "WEB_AGENT_COMPACTION_THRESHOLD"
        )


def _normalize_paths(config: WebAgentConfig, *, base_dir: Path) -> None:
    config.data_dir = _resolve_path(config.data_dir, base_dir=base_dir)
    config.sessions_db = (
        _resolve_path(config.sessions_db, base_dir=base_dir) if config.sessions_db else None
    )
    config.shell_workdir = (
        _resolve_path(config.shell_workdir, base_dir=base_dir) if config.shell_workdir else None
    )
    for skill in config.skills:
        skill.path = _resolve_path(skill.path, base_dir=base_dir)
    for server in config.mcp_servers:
        if server.cwd is not None:
            server.cwd = _resolve_path(server.cwd, base_dir=base_dir)


def _resolve_path(path: Path, *, base_dir: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded
    return (base_dir / expanded).resolve()


def _env_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _require_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _positive_int(value: Any, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return parsed
