# Local Web Agent

This example adds a small cross-platform web agent on top of the Agents SDK. It keeps
the SDK framework unchanged and composes public APIs for chat, sessions, optional
compaction, optional MCP servers, and optional local skills.

## Run

From the repository root:

```bash
uv run python -m examples.web_agent --host 127.0.0.1 --port 8008
```

If your environment is already synced and `python` can import `agents`, this also works:

```bash
python -m examples.web_agent --host 127.0.0.1 --port 8008
```

Open `http://127.0.0.1:8008`.

## Skill and MCP demo

This example includes a local skill and stdio MCP server demo in `examples/web_agent/demo`.
Run it from the repository root with:

```bash
uv run python -m examples.web_agent --config examples/web_agent/demo/web-agent-demo.json --host 127.0.0.1 --port 8008
```

Then ask:

- `Use the briefing-writer skill to summarize: launch is green, docs are pending, owner is Mei.`
- `Use the demo-policy MCP server to look up the session policy.`

The demo uses Chat Completions mode by default so OpenAI-compatible endpoints can run it with
`OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `OPENAI_DEFAULT_MODEL`. The demo MCP server is a local
Python stdio server, so it does not require Node or network access.

## Configuration

Pass a JSON config file with `--config path/to/web-agent.json`, or set `WEB_AGENT_CONFIG`.

```json
{
  "name": "Local Web Agent",
  "model": "gpt-5.4-mini",
  "model_api": "responses",
  "tracing_disabled": false,
  "instructions": "Answer clearly and use tools only when helpful.",
  "data_dir": ".web-agent-data",
  "compaction": {
    "enabled": true,
    "auto": false,
    "model": "gpt-4.1",
    "candidate_threshold": 10,
    "mode": "auto"
  },
  "skills": [
    {
      "name": "csv-workbench",
      "description": "Analyze CSV files and summarize numeric data.",
      "path": "examples/tools/skills/csv-workbench"
    }
  ],
  "mcp_servers": [
    {
      "name": "filesystem",
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
      "require_approval": "always"
    }
  ]
}
```

Environment overrides:

- `WEB_AGENT_MODEL`
- `WEB_AGENT_MODEL_API` (`responses` or `chat_completions`)
- `WEB_AGENT_TRACING_DISABLED`
- `WEB_AGENT_NAME`
- `WEB_AGENT_INSTRUCTIONS`
- `WEB_AGENT_DATA_DIR`
- `WEB_AGENT_SESSIONS_DB`
- `WEB_AGENT_COMPACTION_ENABLED`
- `WEB_AGENT_COMPACTION_AUTO`
- `WEB_AGENT_COMPACTION_MODEL`
- `WEB_AGENT_COMPACTION_THRESHOLD`

## Notes

Local shell skills are disabled unless configured. When enabled, shell tool calls require
approval by default.

SDK tracing is enabled by default for Responses mode. In Chat Completions mode it defaults to
disabled because OpenAI-compatible endpoints commonly use provider-specific API keys.

The separate `E:\SSSClaude006` project was reviewed only for product ideas. Features such
as workspace `.agent` metadata, task panels, team/subagent state, workflow YAML, ZeroMQ
tools, and transcripts should be added only after explicit confirmation.
