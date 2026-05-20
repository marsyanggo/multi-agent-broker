# multi-agent-broker — Project Target

## 專案目標
建立 LLM-agnostic 的跨機器多 agent 協作平台。完整設計見 `architcture.md`。

Phase 1 鎖定最小可運行核心：**agent + message + task**。先把兩台 Claude Code 互通跑起來，再擴展其他 LLM 接入與進階功能。

---

## Phase 1 範圍

### Scope（做）
- 中央 broker：FastAPI + SQLite + WebSocket Hub
- MCP server 接入（Claude Code 原生 MCP stdio）
- 9 個核心 MCP tools：
  - Agent 發現：`list_agents`, `get_agent_info`, `report_status`
  - 訊息：`send_message`, `get_messages`（不做 broadcast）
  - 任務：`create_task`, `claim_task`, `update_task`, `list_tasks`

### Out of scope（Phase 1 不做）
- Channels（頻道 / broadcast）
- Shared context / 程式碼片段共享
- Python SDK 與其他 LLM 接入（OpenAI / Ollama / LangChain adapters）
- HTTP REST polling client 範例
- TLS / wss / JWT / 外網部署
- Web dashboard

### 首發 Demo 場景
兩台 Claude Code（同網段）透過 broker：
1. 互相 `list_agents` 看到對方
2. `send_message` + `get_messages` 互傳訊息
3. Lead 端 `create_task`，Worker 端 `list_tasks` → `claim_task` → `update_task` 完整跑通
4. Kill 一台 MCP server，重啟後離線訊息自動投遞

---

## 關鍵決策（已鎖定）

| 項目 | 決定 |
|------|------|
| 專案名稱 | `multi-agent-broker` |
| Python package | `mab` |
| Env var 前綴 | `MAB_` |
| CLI 入口 | `mab-broker`, `mab-agent` |
| 預設 port | `8420` |
| 心跳間隔 | 30 秒 |
| 訊息 TTL | 7 天（未投遞或未讀超過 7 天清除） |
| SQLite 位置 | `~/.multi-agent-broker/db.sqlite`（可用 `MAB_DB_PATH` 覆寫） |
| Agent name 衝突 | 預設報錯要求改名；`mab-agent --auto-suffix` 自動加序號 |
| Push 限制處理 | MCP server 攔截所有 tool response，附加 `_pending_messages` count 提示 LLM |
| 認證 | API key + SHA-256 hash 比對 |

---

## Phase 1 Sub-tasks（順序開發）

- [x] **T1.** Project skeleton — `pyproject.toml`, 目錄結構, CLI entry points
- [x] **T2.** Shared models + protocol — Agent / Message / Task Pydantic models + WS envelope
- [x] **T3.** SQLite schema + CRUD — 含 atomic task claim（避免多 worker 競態）
- [x] **T4.** FastAPI app + API key auth middleware
- [x] **T5.** REST routes — agents / messages / tasks 三組
- [x] **T6.** WebSocket Hub — 連線註冊、路由、心跳、離線訊息暫存 7 天
- [x] **T7.** MCP broker_client — WS 客戶端 + 指數退避重連
- [x] **T8.** MCP server + 9 個 tools
- [x] **T9.** Pending-count 攔截器 — tool response 注入 `_pending_messages`
- [x] **T10.** 整合測試 — localhost 兩個 MCP server 跑通 demo 4 個情境

---

## 後續 Phase（暫定，待 Phase 1 完成後規劃）

- **Phase 2**：Channels + shared context + broadcast
- **Phase 3**：Python SDK + OpenAI / Ollama / LangChain adapters
- **Phase 4**：外網部署（TLS / wss / JWT / IP allowlist）
- **Phase 5**：Web dashboard + 訊息全文檢索
