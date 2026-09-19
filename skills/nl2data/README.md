# nl2data Skill · 宿主注册指南

本目录是 nl2data 的可分发 Skill 包（SKILL.md 是宿主 LLM 的调用契约）。
本文件回答一个问题：**如何在我的 MCP 宿主里把 nl2data 挂起来。**

## 前置条件

1. 已有 nl2data 仓库并完成 `uv sync`（Python ≥3.11）；
2. 数据已摄取并构建产物（`profile --all` → `cards build --all` → `index build`）；
3. 六个凭证环境变量已备好（模板见 [config-template.sh](config-template.sh)）。

## mcpServers 配置片段（T17 交付样例的落地版）

```json
{
  "mcpServers": {
    "nl2data": {
      "command": "uv",
      "args": ["--directory", "<nl2data 仓库绝对路径>", "run", "nl2data", "mcp", "serve"],
      "env": {
        "LLM_BASE_URL": "<LLM 网关地址>",
        "LLM_API_KEY": "<LLM 密钥>",
        "LLM_MODEL": "<模型名>",
        "EMB_BASE_URL": "<embedding 网关地址>",
        "EMB_API_KEY": "<embedding 密钥>",
        "EMB_MODEL": "<embedding 模型名>"
      }
    }
  }
}
```

三个要点：

- **`env` 块必须显式携带六个变量**：MCP 客户端以 stdio 启动服务器时默认只透传
  安全白名单环境变量，宿主会话里 export 过的值不会自动到达 nl2data；
- `command` 建议写绝对路径（如 `D:\...\uv.exe` 或 venv 内
  `python -m nl2data.cli mcp serve`），宿主的 PATH 通常与你的终端不同；
- 配置文件不在仓库根时，在 `env` 里加 `"NL2DATA_CONFIG": "<config.yaml 绝对路径>"`。

## 常见宿主的注册位置

| 宿主 | 位置 / 命令 |
|---|---|
| Claude Code | `claude mcp add nl2data -s user -- uv --directory <仓库> run nl2data mcp serve`（密钥经 `-e KEY=value` 逐个追加，或编辑 `~/.claude.json` 的 `mcpServers`） |
| Cursor | 项目级 `.cursor/mcp.json` / 全局 `~/.cursor/mcp.json`，填上面的 JSON 片段 |
| zcode | 工作区或用户级 MCP 服务器配置，填同样的 `mcpServers` 片段 |
| 其他 JSON 配置宿主 | 任何接受 `mcpServers` 标准结构的客户端，直接粘贴片段 |

## 挂好之后

对宿主说「用 nl2data 查一下……」即可；宿主 LLM 会按 SKILL.md 的工作流先调
`nl2data_status` 诊断，缺配置时向你索要。只有 stdio 传输（无 SSE/HTTP、无认证层，
单机个人使用）；工具面只有 `nl2data_status` / `nl2data_list_tables` / `nl2data_ask`
三个只读工具。
