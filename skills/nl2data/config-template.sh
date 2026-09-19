# nl2data 凭证环境变量模板（goals.md §3 决策 3 / 决策 8）
#
# 用法（二选一）：
#   1. 复制为仓库外的私有文件（如 ~/.nl2data-env.sh），填值后 `source` 它，
#      再在同一终端启动宿主或 CLI；
#   2. 直接把六个值填入 MCP 宿主 mcpServers 配置的 "env" 块（推荐，见 README.md）。
#
# 红线：填好的值不要提交 git、不要写入项目内任何文件、不要贴进对话记录。
# 本文件只含占位符，可以安全分发。

export LLM_BASE_URL=""   # LLM 网关（OpenAI 兼容）。示例（硅基流动）: https://api.siliconflow.cn/v1
export LLM_API_KEY=""    # LLM 密钥（sk- 前缀格式；此处永远只留空占位，勿填真实值）
export LLM_MODEL=""      # 模型名。示例（硅基流动）: deepseek-ai/DeepSeek-V3
export EMB_BASE_URL=""   # embedding 网关（OpenAI 兼容）。示例（硅基流动）: https://api.siliconflow.cn/v1
export EMB_API_KEY=""    # embedding 密钥
export EMB_MODEL=""      # embedding 模型名。示例（硅基流动）: BAAI/bge-m3
