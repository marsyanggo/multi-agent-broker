# 跨機器多 LLM Agent 協作系統 — 架構設計文件

## 1. 專案目標

建立一個 **LLM-agnostic** 的多 agent 協作平台，讓不同機器、不同 LLM 的 agent 能夠互相溝通、協作。

**支援的 LLM / Agent 框架：**
- Claude Code（via MCP stdio）
- Gemini CLI（via MCP stdio）
- OpenAI GPT（via Function Calling adapter）
- Local LLM — Ollama, vLLM, llama.cpp（via Python agent loop + REST client）
- LangChain / CrewAI / AutoGen（via Python tool wrapper）
- 任何能發 HTTP 的自訂 agent

**核心需求：**
- 跨機器、跨 LLM 即時訊息傳遞（同一內網 / 未來擴展到公網）
- 支援 Lead-Worker（主控分派）與 Peer-to-Peer（對等通訊）兩種協作模式
- 任務建立、認領、追蹤
- 程式碼片段 / 分析結果等上下文共享
- 多種接入方式：MCP、REST API、Python SDK、WebSocket 直連

---

## 2. 系統架構總覽

```
┌──────────────────────────────────────────────────────────────────────┐
│                    CENTRAL BROKER (任一台固定 IP 機器)                  │
│                    FastAPI + SQLite + WebSocket Hub                    │
│                                                                       │
│  ┌────────────┐ ┌────────────┐ ┌──────────┐ ┌───────────┐           │
│  │ Agent      │ │ Message    │ │ Task     │ │ Context   │           │
│  │ Registry   │ │ Router     │ │ Manager  │ │ Store     │           │
│  └────────────┘ └────────────┘ └──────────┘ └───────────┘           │
│  ┌────────────┐ ┌────────────┐ ┌──────────┐ ┌───────────────────┐  │
│  │ WebSocket  │ │ Auth       │ │ Channel  │ │ REST API          │  │
│  │ Hub        │ │ Middleware │ │ Manager  │ │ (通用接入點)        │  │
│  └─────┬──────┘ └────────────┘ └──────────┘ └────────┬──────────┘  │
└────────┼─────────────────────────────────────────────┼──────────────┘
         │ WebSocket                                    │ HTTP REST
         │ (persistent)                                │ (polling/on-demand)
    ┌────┼────────────┬──────────────┐                 │
    │    │            │              │            ┌────┼──────────────┐
    │    │            │              │            │    │              │
┌───┼────┼──┐  ┌─────┼────┐  ┌─────┼────┐  ┌───┼────┼──┐  ┌───────┼──┐
│Machine 1  │  │Machine 2  │  │Machine 3  │  │Machine 4  │  │Machine 5 │
│           │  │           │  │           │  │           │  │          │
│MCP Server │  │MCP Server │  │MCP Server │  │Python SDK │  │HTTP REST │
│(stdio)    │  │(stdio)    │  │(stdio)    │  │Client     │  │Client    │
│    │      │  │    │      │  │    │      │  │    │      │  │    │     │
│Claude Code│  │Gemini CLI │  │Claude Code│  │Ollama +   │  │GPT Agent │
│           │  │           │  │           │  │Agent Loop │  │(any lang)│
└───────────┘  └───────────┘  └───────────┘  └───────────┘  └──────────┘
```

### 接入方式對照

| LLM / 框架 | 接入方式 | 連線類型 | 說明 |
|------------|---------|---------|------|
| **Claude Code** | MCP Server (stdio) | WebSocket | 原生 MCP 支援 |
| **Gemini CLI** | MCP Server (stdio) | WebSocket | Gemini CLI 也支援 MCP |
| **OpenAI GPT** | Python SDK + Function Calling | WebSocket or REST | 把 broker 工具包成 OpenAI functions |
| **Local LLM (Ollama)** | Python SDK + Agent Loop | WebSocket or REST | 自訂 agent loop 呼叫 SDK |
| **LangChain / CrewAI** | Python SDK Tool Wrapper | WebSocket or REST | 包成 LangChain BaseTool |
| **任何語言** | HTTP REST API 直接呼叫 | HTTP Polling | 最通用，無依賴 |

### 設計原則

| 原則 | 說明 |
|------|------|
| LLM-Agnostic | Broker 不關心連入的是什麼 LLM，只認 agent identity |
| 多接入方式 | MCP (stdio)、WebSocket 直連、REST polling 三種方式並存 |
| 向外連接 | Agent 主動連 broker，不需開 inbound port（NAT-friendly） |
| stdio MCP | Claude Code / Gemini CLI 原生支援的模式 |
| WebSocket 雙向 | 低延遲即時投遞，單一持久連線 |
| SQLite 持久化 | 零運維，2-3 台機器足夠用 |
| 離線投遞 | Agent 離線時訊息暫存，重連後自動推送 |

---

## 3. 元件說明

### 3.1 Central Broker（中央代理伺服器）

運行在一台固定 IP 的機器上，負責所有 agent 的協調。

**職責：**
- Agent 註冊 / 發現 / 心跳管理
- 訊息路由與持久化
- 任務生命週期管理
- 共享上下文存儲
- 頻道管理

**技術：**
- FastAPI（HTTP REST + WebSocket）
- SQLite（aiosqlite 非同步操作）
- uvicorn（ASGI server）

### 3.2 MCP Server（Claude Code / Gemini CLI 接入）

每台機器上運行的 stdio 程式，作為支援 MCP 的 LLM CLI 與 Broker 之間的橋樑。

**適用於：** Claude Code, Gemini CLI

**職責：**
- 維持與 broker 的 persistent WebSocket 連線（含斷線重連）
- 將 LLM 的 tool call 轉換為 broker API 請求
- 本地訊息緩衝（收到的訊息等待 LLM 來拉取）
- Agent 身分管理

**技術：**
- `mcp` Python SDK（Anthropic 官方）
- `websockets` 客戶端
- asyncio 背景任務

### 3.3 Python SDK Client（通用 LLM 接入）

提供 Python library 讓任何 LLM agent 框架直接接入 broker。

**適用於：** OpenAI GPT, Local LLM (Ollama/vLLM), LangChain, CrewAI, AutoGen, 自訂 agent

**使用方式：**

```python
from claude_collab import CollabClient

# 初始化客戶端
client = CollabClient(
    broker_url="ws://192.168.1.100:8420/api/v1/ws",
    agent_name="gpt-worker-1",
    api_key="cc-ak-xxxx",
    capabilities=["python", "data-analysis"]
)

# 連接
await client.connect()

# 發送訊息
await client.send_message(to="builder-1", content="分析完成，結果如下...")

# 接收訊息
messages = await client.get_messages()

# 建立任務
task = await client.create_task(title="跑測試", assigned_to="claude-agent-1")

# 認領任務
await client.claim_task(task_id="xxx")

# 回報完成
await client.update_task(task_id="xxx", status="completed", result="全部通過")
```

**OpenAI Function Calling 整合範例：**

```python
from claude_collab.adapters.openai import get_collab_tools, handle_tool_call

# 取得 OpenAI function 定義
tools = get_collab_tools()  # 回傳 OpenAI tools format

# 在 GPT 對話 loop 中
response = openai.chat.completions.create(
    model="gpt-4o",
    messages=messages,
    tools=tools  # 包含 send_message, get_messages, create_task 等
)

# 處理 tool call
if response.choices[0].message.tool_calls:
    for tool_call in response.choices[0].message.tool_calls:
        result = await handle_tool_call(client, tool_call)
```

**LangChain 整合範例：**

```python
from claude_collab.adapters.langchain import get_collab_tools

# 取得 LangChain tools
tools = get_collab_tools(broker_url="ws://...", api_key="cc-ak-xxxx")

# 直接給 agent 使用
agent = initialize_agent(tools=tools, llm=llm, ...)
```

**Ollama / Local LLM 整合範例：**

```python
from claude_collab import CollabClient
from claude_collab.adapters.agent_loop import AgentLoop

# 建立 agent loop（自動處理訊息收發 + 任務執行）
loop = AgentLoop(
    client=CollabClient(...),
    llm_fn=my_ollama_chat_function,  # 你的 LLM 呼叫函數
    system_prompt="你是一個專門跑測試的 agent...",
    poll_interval=5  # 每 5 秒檢查新訊息/任務
)

await loop.run()  # 開始監聽並自動回應
```

### 3.4 HTTP REST Client（最通用接入）

不需要任何 SDK，直接用 HTTP 呼叫 broker API。適用於任何語言。

**適用於：** Node.js, Go, Rust, Shell script, 或任何能發 HTTP 的環境

```bash
# 註冊 agent
curl -X POST http://192.168.1.100:8420/api/v1/auth/register \
  -H "X-API-Key: cc-ak-xxxx" \
  -d '{"name": "node-worker", "capabilities": ["javascript"]}'

# 發送訊息
curl -X POST http://192.168.1.100:8420/api/v1/messages \
  -H "Authorization: Bearer <token>" \
  -d '{"to_agent": "builder-1", "content": "hello"}'

# 拉取訊息
curl http://192.168.1.100:8420/api/v1/messages/my-agent-id \
  -H "Authorization: Bearer <token>"
```

### 3.5 Shared Protocol（共享協議）

所有接入方式共用的資料格式定義，確保不同 LLM agent 之間的互操作性。

---

## 4. 通訊協議

### 4.1 WebSocket 訊息格式

所有 WebSocket 訊息使用統一的 JSON envelope：

```json
{
    "type": "message | task_event | broadcast | agent_event | ack",
    "id": "uuid",
    "from_agent": "agent_id",
    "to_agent": "agent_id 或 null（廣播時）",
    "channel": "channel_name 或 null",
    "payload": { },
    "timestamp": "2026-05-19T10:30:00Z"
}
```

### 4.2 訊息類型

| type | payload 內容 | 說明 |
|------|-------------|------|
| `message` | `{content, content_type, reply_to}` | 直接訊息 |
| `broadcast` | `{content, channel}` | 頻道廣播 |
| `task_event` | `{task_id, event, task}` | 任務生命週期事件 |
| `agent_event` | `{event, agent}` | Agent 上下線通知 |
| `ack` | `{ack_id}` | 收訊確認 |

### 4.3 訊息流向圖

**場景一：Claude Code → Claude Code（MCP 對 MCP）**
```
Agent A (Claude Code)     MCP Server A        Broker         MCP Server B     Agent B (Claude Code)
      │                        │                 │                 │                  │
      │ tool: send_message     │                 │                 │                  │
      ├───────────────────────>│                 │                 │                  │
      │                        │ WS: message     │                 │                  │
      │                        ├────────────────>│                 │                  │
      │                        │                 │ persist + route │                  │
      │                        │                 │ WS: message     │                  │
      │                        │                 ├────────────────>│                  │
      │                        │ ack             │                 │ queue locally    │
      │                        │<────────────────│                 │                  │
      │ result: {sent: true}   │                 │                 │                  │
      │<───────────────────────│                 │                 │                  │
      │                        │                 │                 │ tool: get_msgs   │
      │                        │                 │                 │<─────────────────│
      │                        │                 │                 │ return messages  │
      │                        │                 │                 ├─────────────────>│
```

**場景二：Claude Code → GPT Agent（MCP 對 Python SDK）**
```
Agent A (Claude Code)     MCP Server A        Broker         Python SDK       GPT Agent Loop
      │                        │                 │                 │                  │
      │ tool: send_message     │                 │                 │                  │
      ├───────────────────────>│                 │                 │                  │
      │                        │ WS: message     │                 │                  │
      │                        ├────────────────>│                 │                  │
      │                        │                 │ WS: message     │                  │
      │                        │                 ├────────────────>│                  │
      │                        │                 │                 │ callback/poll    │
      │                        │                 │                 ├─────────────────>│
      │                        │                 │                 │                  │
      │                        │                 │                 │ GPT decides to   │
      │                        │                 │                 │ reply via SDK    │
      │                        │                 │                 │<─────────────────│
      │                        │                 │ WS: message     │                  │
      │                        │                 │<────────────────│                  │
      │                        │ WS: message     │                 │                  │
      │                        │<────────────────│                 │                  │
      │ (next get_messages)    │                 │                 │                  │
      │<───────────────────────│                 │                 │                  │
```

**場景三：Local LLM → 任何 Agent（REST API polling）**
```
Ollama Agent          HTTP Client         Broker            Any other agent
      │                    │                 │                      │
      │ decide to send     │                 │                      │
      ├───────────────────>│                 │                      │
      │                    │ POST /messages   │                      │
      │                    ├────────────────>│  route via WS/store  │
      │                    │ 200 OK          │─────────────────────>│
      │                    │<────────────────│                      │
      │                    │                 │                      │
      │ (poll for replies) │                 │                      │
      │ GET /messages/me   │                 │                      │
      ├───────────────────>├────────────────>│                      │
      │                    │ [messages]      │                      │
      │<───────────────────│<────────────────│                      │
```

---

## 5. MCP Tools（Agent 可用的工具清單）

### 5.1 Agent 發現

| Tool | 參數 | 說明 |
|------|------|------|
| `list_agents` | — | 列出所有在線 agent 及其狀態 |
| `get_agent_info` | `agent_id` | 取得特定 agent 詳情 |
| `report_status` | `status, current_task, details` | 回報自己的狀態 |

### 5.2 訊息傳遞

| Tool | 參數 | 說明 |
|------|------|------|
| `send_message` | `to_agent, content, content_type, reply_to` | 發送直接訊息 |
| `broadcast_message` | `content, channel, content_type` | 廣播到頻道 |
| `get_messages` | `limit, since, from_agent, channel` | 拉取收到的訊息 |

### 5.3 任務管理

| Tool | 參數 | 說明 |
|------|------|------|
| `create_task` | `title, description, assigned_to, priority, context` | 建立任務 |
| `claim_task` | `task_id` | 認領未指派的任務 |
| `update_task` | `task_id, status, result, notes` | 更新任務進度/結果 |
| `list_tasks` | `status, assigned_to, created_by` | 列出任務 |

### 5.4 上下文共享

| Tool | 參數 | 說明 |
|------|------|------|
| `share_context` | `content, title, content_type, tags` | 分享程式碼/diff/分析 |
| `get_shared_context` | `file_id` | 取得共享內容 |
| `list_shared_contexts` | `tags, from_agent, limit` | 列出共享內容 |

### 5.5 頻道管理

| Tool | 參數 | 說明 |
|------|------|------|
| `join_channel` | `channel` | 加入頻道 |
| `leave_channel` | `channel` | 離開頻道 |

---

## 6. 資料模型

### 6.1 Agent

```python
class Agent:
    id: str              # 8 字元短 UUID
    name: str            # 人類友善名稱，如 "builder-1"
    machine_id: str      # hostname
    capabilities: list   # ["python", "frontend", "testing"]
    status: str          # online / busy / idle / offline
    current_task: str    # 正在處理的 task_id
    channels: list       # 加入的頻道列表
    registered_at: datetime
    last_heartbeat: datetime
    api_key_hash: str    # 認證用
```

### 6.2 Message

```python
class Message:
    id: str
    from_agent: str
    to_agent: str | None     # None = 廣播
    channel: str | None
    content: str
    content_type: str        # text/plain, text/markdown, application/json
    reply_to: str | None
    created_at: datetime
    delivered: bool
    delivered_at: datetime | None
```

### 6.3 Task

```python
class Task:
    id: str
    title: str
    description: str
    created_by: str
    assigned_to: str | None  # None = 開放認領
    status: str              # pending / assigned / in_progress / completed / failed / blocked
    priority: str            # low / normal / high / urgent
    context_ids: list        # 關聯的 shared file IDs
    result: str | None       # 完成時的輸出
    notes: list              # 進度備註
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
```

### 6.4 SharedFile

```python
class SharedFile:
    id: str
    title: str
    content: str
    content_type: str
    from_agent: str
    tags: list
    size_bytes: int
    created_at: datetime
```

---

## 7. 專案目錄結構

```
claude-collab/
├── pyproject.toml                  # 套件設定、依賴管理
├── README.md                       # 快速上手指南
├── .env.example                    # 環境變數範例
│
├── src/claude_collab/
│   ├── __init__.py                 # 匯出 CollabClient
│   ├── config.py                   # 全域設定（Pydantic Settings）
│   ├── client.py                   # ★ CollabClient — 通用 Python SDK 客戶端
│   │
│   ├── broker/                     # === Central Broker ===
│   │   ├── __init__.py
│   │   ├── app.py                  # FastAPI 應用 + main()
│   │   ├── auth.py                 # API key 驗證
│   │   ├── db.py                   # SQLite schema + CRUD
│   │   ├── models.py              # Broker 側 Pydantic models
│   │   ├── websocket.py           # WebSocket 連線管理 + 路由
│   │   ├── scheduler.py           # 心跳超時、清理任務
│   │   └── routes/
│   │       ├── __init__.py
│   │       ├── agents.py          # /api/v1/agents
│   │       ├── messages.py        # /api/v1/messages
│   │       ├── tasks.py           # /api/v1/tasks
│   │       ├── files.py           # /api/v1/files
│   │       └── channels.py        # /api/v1/channels
│   │
│   ├── mcp_server/                 # === MCP 接入（Claude Code / Gemini CLI）===
│   │   ├── __init__.py
│   │   ├── server.py              # MCP stdio 入口
│   │   ├── tools.py               # MCP tool 實作
│   │   ├── broker_client.py       # WebSocket 客戶端 + 重連邏輯
│   │   └── message_queue.py       # 本地訊息緩衝
│   │
│   ├── adapters/                   # === LLM 框架 Adapters ===
│   │   ├── __init__.py
│   │   ├── openai_adapter.py      # OpenAI Function Calling 整合
│   │   ├── langchain_adapter.py   # LangChain BaseTool 包裝
│   │   ├── crewai_adapter.py      # CrewAI Tool 包裝
│   │   └── agent_loop.py          # 通用 Agent Loop（適用 Ollama / 任何 LLM）
│   │
│   └── shared/                     # === 共享協議 ===
│       ├── __init__.py
│       ├── models.py              # 共用 Pydantic models
│       └── protocol.py           # WS 訊息類型定義
│
├── scripts/
│   ├── start_broker.py            # 快速啟動 broker
│   └── generate_api_key.py        # 產生 agent API key
│
├── tests/
│   ├── test_broker/
│   │   ├── test_routes.py
│   │   ├── test_websocket.py
│   │   └── test_db.py
│   ├── test_mcp_server/
│   │   ├── test_tools.py
│   │   └── test_broker_client.py
│   ├── test_adapters/
│   │   ├── test_openai_adapter.py
│   │   ├── test_langchain_adapter.py
│   │   └── test_agent_loop.py
│   └── conftest.py
│
└── examples/
    ├── mcp_config.json            # Claude Code / Gemini CLI 設定範例
    ├── agent_config.yaml          # Agent 設定範例
    ├── openai_agent.py            # GPT agent 使用範例
    ├── ollama_agent.py            # Local LLM agent 使用範例
    ├── langchain_agent.py         # LangChain 整合範例
    └── simple_http_agent.sh       # 純 HTTP REST 使用範例（Shell script）
```

---

## 8. 依賴套件

```toml
[project]
name = "claude-collab"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.110.0",
    "uvicorn[standard]>=0.27.0",
    "websockets>=12.0",
    "pydantic>=2.6.0",
    "pydantic-settings>=2.1.0",
    "aiosqlite>=0.19.0",
    "mcp>=1.0.0",
    "httpx>=0.27.0",
    "pyyaml>=6.0",
]

[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio", "httpx"]

[project.scripts]
collab-broker = "claude_collab.broker.app:main"
collab-agent = "claude_collab.mcp_server.server:main"
```

---

## 9. 使用方式

### 9.1 啟動 Broker（一台機器）

```bash
pip install claude-collab
collab-broker --host 0.0.0.0 --port 8420
```

### 9.2 設定 Agent（每台機器）

方法一：CLI 指令
```bash
claude mcp add collab -- python -m claude_collab.mcp_server.server \
  --name "builder-1" \
  --broker "ws://192.168.1.100:8420/api/v1/ws" \
  --api-key "cc-ak-xxxxxxxxxxxx"
```

方法二：在專案 `.mcp.json` 加入
```json
{
  "mcpServers": {
    "collab": {
      "command": "python",
      "args": ["-m", "claude_collab.mcp_server.server"],
      "env": {
        "COLLAB_AGENT_NAME": "builder-1",
        "COLLAB_BROKER_URL": "ws://192.168.1.100:8420/api/v1/ws",
        "COLLAB_API_KEY": "cc-ak-xxxxxxxxxxxx"
      }
    }
  }
}
```

### 9.3 使用範例

**Lead-Worker 模式：**
```
Lead Agent: create_task(title="修復登入 bug", description="...", priority="high")
Worker Agent: list_tasks(status="pending") → claim_task(task_id) → ... 工作 ... → update_task(status="completed", result="已修復，原因是...")
```

**Peer-to-Peer 模式：**
```
Agent A: list_agents() → send_message(to="agent-b", content="你那邊的 API 測試結果如何？")
Agent B: get_messages() → send_message(to="agent-a", content="全部通過，共 42 個 test cases")
```

---

## 10. 安全性規劃

### Phase 1：內網部署（當前）

- API Key 認證（每個 agent 一組 key）
- Key 在 broker 端以 SHA-256 hash 儲存
- Rate limiting（防止失控迴圈）
- 內容大小限制

### Phase 2：外網部署（未來）

| 項目 | 做法 |
|------|------|
| 傳輸加密 | TLS（Let's Encrypt / Caddy reverse proxy） |
| 連線安全 | wss:// WebSocket Secure |
| 進階認證 | JWT token（短效、自動 rotation） |
| 網路限制 | IP allowlist per agent |
| 資料加密 | 訊息內容 AES-256 加密存儲 |
| 稽核記錄 | 所有操作記錄 audit log |

**設計上的預留：**
- Auth middleware 獨立層（可替換 API key → JWT / mTLS）
- URL 全部可設定（http → https, ws → wss）
- Config 預留 `tls_cert_path`, `tls_key_path` 欄位

---

## 11. 實作排程

| Phase | 工作項目 | 預估時間 | 產出 |
|-------|---------|---------|------|
| 1 | 共享模型 + 協議定義 | 30 min | `shared/models.py`, `shared/protocol.py` |
| 2 | SQLite schema + CRUD | 1 hr | `broker/db.py` |
| 3 | FastAPI 骨架 + Auth | 1 hr | `broker/app.py`, `broker/auth.py` |
| 4 | WebSocket Hub | 2 hr | `broker/websocket.py` |
| 5 | REST API routes | 2 hr | `broker/routes/*.py` |
| 6 | MCP WebSocket 客戶端 | 1.5 hr | `mcp_server/broker_client.py` |
| 7 | MCP Server + Tools | 2 hr | `mcp_server/server.py`, `mcp_server/tools.py` |
| 8 | 整合測試 | 1 hr | 兩個 agent 互通驗證 |

**總計約 11 小時開發時間**

---

## 12. 驗證計畫

1. **Broker 健康檢查** — 啟動後 `GET /health` 回應 200
2. **Agent 註冊** — 啟動兩個 MCP server，broker 顯示 2 agents online
3. **Agent 發現** — Claude Code 中呼叫 `list_agents`，看到對方
4. **訊息傳遞** — Agent A `send_message` → Agent B `get_messages` 收到
5. **任務流程** — 建立 → 認領 → 完成 全流程
6. **斷線重連** — Kill MCP server，重啟後離線訊息自動投遞
7. **廣播** — `broadcast_message` 所有頻道成員都收到

---

## 13. 已知限制與未來方向

**當前限制：**
- Claude Code 無法被「推送中斷」— agent 必須主動呼叫 `get_messages` 才能收到訊息
- 單一 broker 無高可用（2-3 台機器夠用，掛了重啟即可）
- 僅支援文字內容共享（不支援二進位檔案）

**未來可擴展：**
- Web dashboard 監控所有 agent 狀態
- 訊息歷史搜尋（全文檢索）
- Webhook 通知（整合 Slack / Discord）
- 多 broker 叢集（大規模部署）
- Agent capability-based routing（自動根據能力分派任務）


