# Web Agent 使用说明

`web_agent` 是本仓库内的 Web 应用层入口，用来把 OpenAI Agents SDK 的核心能力包装成一个可在浏览器中使用的本地 Agent。它不是一个独立重写的 Agent 框架，而是复用并组合 SDK 已有能力：

- `Agent`、`Runner`、`RunState` 负责 Agent 编排、运行和审批恢复。
- `SQLiteSession` 负责会话持久化。
- `OpenAIResponsesCompactionSession` 负责 Responses API 会话压缩。
- `MCPServerManager` 和各类 MCP server 负责 MCP 工具接入。
- `ShellTool` 和本地 skill 元数据负责本地 skill 加载。

Web UI、会话列表、审批按钮和配置加载属于应用层，代码放在 `web_agent/` 目录下。

## 快速启动

启动前先准备 Python 环境和模型配置。Web Agent 本身不需要前端构建，静态页面已经放在 `web_agent/static/`。

### Windows PowerShell

```powershell
$env:OPENAI_API_KEY = "你的 API Key"
$env:WEB_AGENT_MODEL = "你的模型名"
$env:WEB_AGENT_MODEL_API = "chat_completions"

.\.venv\Scripts\python.exe -m web_agent `
  --config web_agent\demo\web-agent-demo.json `
  --host 127.0.0.1 `
  --port 8008
```

启动后浏览器打开：

```text
http://127.0.0.1:8008
```

### Linux shell

```bash
export OPENAI_API_KEY="你的 API Key"
export WEB_AGENT_MODEL="你的模型名"
export WEB_AGENT_MODEL_API="responses"

python -m web_agent \
  --config web_agent/demo/web-agent-demo.json \
  --host 127.0.0.1 \
  --port 8008
```

如果使用 `uv` 管理环境，也可以把启动命令中的 `python` 换成：

```bash
uv run python
```

## 模型配置

Web Agent 支持两种模型 API：

- `responses`：默认模式，适合 OpenAI Responses API。该模式可以使用 Responses 会话压缩能力。
- `chat_completions`：适合 Chat Completions 兼容服务。该模式下会话仍持久化到 SQLite，但不会启用 Responses compaction session。

常用环境变量：

```text
OPENAI_API_KEY
OPENAI_BASE_URL
OPENAI_DEFAULT_MODEL
WEB_AGENT_MODEL
WEB_AGENT_MODEL_API
WEB_AGENT_TRACING_DISABLED
```

优先级上，`WEB_AGENT_MODEL` 会覆盖 `OPENAI_DEFAULT_MODEL`，`WEB_AGENT_MODEL_API` 会覆盖配置文件中的 `model_api`。

使用 OpenAI 官方 Responses API 时，通常配置：

```bash
export WEB_AGENT_MODEL_API="responses"
export WEB_AGENT_MODEL="gpt-4.1"
```

使用 OpenAI 兼容 Chat Completions 服务时，通常配置：

```bash
export OPENAI_BASE_URL="https://你的兼容服务/v1"
export WEB_AGENT_MODEL_API="chat_completions"
export WEB_AGENT_MODEL="你的模型名"
```

## 配置文件

默认 demo 配置在：

```text
web_agent/demo/web-agent-demo.json
```

最小配置示例：

```json
{
  "name": "Web Agent",
  "model": "gpt-4.1",
  "model_api": "responses",
  "data_dir": "../.web-agent-data/default",
  "instructions": "You are a helpful web agent.",
  "compaction": {
    "enabled": true,
    "auto": true,
    "model": "gpt-4.1",
    "candidate_threshold": 10,
    "mode": "auto"
  },
  "skills": [],
  "mcp_servers": []
}
```

路径字段支持相对路径。相对路径以配置文件所在目录为基准解析，不以启动命令所在目录为基准。

## 会话和数据目录

默认数据目录是当前工作目录下的：

```text
.web-agent-data/
```

可以通过配置文件或环境变量指定：

```json
{
  "data_dir": "../../.web-agent-data/demo",
  "sessions_db": "../../.web-agent-data/demo/sessions.sqlite"
}
```

或：

```powershell
$env:WEB_AGENT_DATA_DIR = "E:\agent-data\web-agent"
$env:WEB_AGENT_SESSIONS_DB = "E:\agent-data\web-agent\sessions.sqlite"
```

数据目录内会保存：

- `sessions.sqlite`：SDK session 历史。
- `sessions.json`：Web UI 会话元数据。
- `pending/`：等待审批的 `RunState` 和工具调用中断信息。

这些文件是运行态数据，不应提交到 Git。

## MCP 配置

MCP server 配置支持 `stdio`、`sse`、`streamable_http` 三种 transport。

stdio 示例：

```json
{
  "mcp_servers": [
    {
      "name": "demo-policy",
      "transport": "stdio",
      "command": "{python}",
      "args": ["mcp_server.py"],
      "cwd": ".",
      "cache_tools": true,
      "require_approval": "never"
    }
  ]
}
```

`"{python}"` 会在加载配置时替换成当前 Python 解释器，便于 Windows 和 Linux 使用同一份配置。

HTTP MCP 示例：

```json
{
  "mcp_servers": [
    {
      "name": "remote-tools",
      "transport": "streamable_http",
      "url": "http://127.0.0.1:9000/mcp",
      "headers": {},
      "timeout": 30,
      "cache_tools": true,
      "require_approval": "always"
    }
  ]
}
```

稳定性相关配置：

```json
{
  "mcp_strict": false,
  "mcp_connect_in_parallel": false,
  "mcp_connect_timeout_seconds": 10,
  "mcp_cleanup_timeout_seconds": 10,
  "convert_mcp_schemas_to_strict": true,
  "include_server_in_tool_names": true
}
```

默认 `mcp_strict` 为 `false`，单个 MCP server 连接失败时会丢弃失败 server，尽量保持 Web Agent 可启动。如果生产环境要求所有 server 必须成功连接，可以改为 `true`。

## Skill 配置

本地 skill 配置示例：

```json
{
  "skills": [
    {
      "name": "briefing-writer",
      "description": "Create compact operational briefings from notes and task lists.",
      "path": "skills/briefing-writer"
    }
  ]
}
```

`path` 可以指向 skill 目录，也可以直接指向 `SKILL.md` 文件。指向目录时，Web Agent 会读取目录下的 `SKILL.md`。

不同模型 API 下 skill 的接入方式不同：

- `responses`：通过 SDK `ShellTool` 的本地 skill 元数据暴露。
- `chat_completions`：通过 `load_local_skill` function tool 按需读取 `SKILL.md`。

如果开启 shell tool，建议保留默认审批：

```json
{
  "shell_needs_approval": true
}
```

## 安全建议

默认只监听 `127.0.0.1`。除非前面有认证、网关或内网隔离，不建议直接监听 `0.0.0.0`。

Web Agent 可以接入 MCP 和 shell skill。生产配置里应明确：

- 哪些 MCP 工具需要审批。
- shell 工作目录 `shell_workdir` 是否限制在预期工程目录内。
- `shell_needs_approval` 是否保持为 `true`。
- 数据目录是否放在可备份、可清理的位置。

## 离线环境说明

本项目要求 Windows 和 Linux 都可用，不考虑 macOS。新增依赖时必须先满足离线可用：

- Windows 优先从 `D:\PythonRepo` 加载 wheel。
- 如果 `D:\PythonRepo` 缺少所需 wheel，先下载到 `D:\PythonRepo`，再从该目录安装。
- 下载新 wheel 时，需要同时准备 Linux 和 Windows、Python 3.8、3.10、3.13、3.14 的目标产物。纯 Python `py3-none-any` wheel 可以覆盖多个 Python 版本。
- 优先使用国内镜像源，国内镜像没有目标产物时再使用国外源。
- Linux 离线部署时，应把同一批 Linux wheel 同步到目标机器的本地 wheelhouse，再通过 `--find-links` 或环境内既有包缓存安装。

Web Agent 当前不引入独立前端构建依赖，浏览器页面由 Python HTTP 服务直接提供。

## 常用命令

启动 demo：

```powershell
.\.venv\Scripts\python.exe -m web_agent --config web_agent\demo\web-agent-demo.json --host 127.0.0.1 --port 8008
```

只运行 Web Agent 测试：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_web_agent.py -q
```

运行全量检查：

```bash
make format
make lint
make typecheck
make tests
```

## 常见问题

端口已占用：

- 换一个端口，例如 `--port 8013`。

模型请求失败：

- 检查 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`WEB_AGENT_MODEL`、`WEB_AGENT_MODEL_API` 是否匹配当前服务。
- 如果使用 Chat Completions 兼容服务，设置 `WEB_AGENT_MODEL_API=chat_completions`。

MCP server 启动失败：

- 检查 `command`、`args`、`cwd` 是否能在 Windows 和 Linux 上运行。
- 对 Python stdio server，优先使用 `"{python}"`，避免写死平台相关解释器路径。
- 生产环境需要强校验时设置 `mcp_strict=true`。

skill 没有生效：

- 检查 `path` 是否相对配置文件目录可解析。
- 检查 skill 目录下是否存在 `SKILL.md`。
- Chat Completions 模式下，模型需要调用 `load_local_skill` 后才能读取 skill 内容。
