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

## Phase 1.5 — Deployment

目標：在另一台 Linux PC 上一鍵跑起來。Native uv + systemd user service，broker 端用 sudo 一次（`loginctl enable-linger`），之後 day-to-day 全部不用 sudo。

- [x] **D1.** `deploy/install.sh` — uv 檢查 → uv sync → systemd user unit → enable + start → /health 等待
- [x] **D2.** `deploy/uninstall.sh` — 拆服務 unit，保留 DB
- [x] **D3.** `deploy/README.md` — 安裝 / ops / 更新 / 移除 文件
- [x] **D4a.** 在 192.168.1.212 跑 `./deploy/install.sh` 成功，broker 起來
- [x] **D4b.** `gen-key --name claude-mac` 拿 API key，`claude mcp add -s user` 加進 `~/.claude.json`
- [x] **D4c.** 重啟 Claude Code 後驗證 `list_agents` 看得到 `claude-mac` online
- [x] **D4d.** Cross-machine demo：Mac 端 Claude Code `send_message` / `create_task` 跑通對 broker 的完整 lifecycle
- [x] **D5.** 修 `install.sh` / `deploy/README.md` — gen-key 範例帶上 `MAB_DB_PATH`，避免 broker 跟 gen-key 寫到不同 DB
- [x] **D6.** `deploy/update.sh` — 一鍵更新（dirty-tree guard + ff-only + uv sync + systemctl restart + /health 等待 + rollback hint）

---

## Phase 2.1 — Capability-based routing

目標：以「model 識別」為主軸的 capability 系統，讓 task 能精準分派或只廣播給合適的 agent。

### 決策（已鎖定）

| 項目 | 決定 |
|------|------|
| Capability 字串格式 | Prefix-structured：`model:<exact>`, `family:<x>`, `tier:<x>`, `provider:<x>`, 加自由 tag（vision, audio...）|
| Routing 策略 | Filter broadcast — broker 只 push `task_event:created` 給匹配的 online agent，仍以 open pool + atomic claim 競爭 |
| Match 語意 | `required_all`（AND）+ `required_any`（OR）兩欄分開，AND-of-AND-AND-of-OR |
| Agent 宣告途徑 | gen-key 設 default + `mab-agent --model X` runtime 覆寫 |
| 空要求 | 廣播給所有 online agent（向後相容 Phase 1） |
| 零匹配 | task 仍創建（status pending、孤兒），新 agent 連線後可從 `list_tasks` 撈 |
| claim 驗 cap | 是 — 即使知道 task id，capability 不符合也 403（不是純廣播 filter） |
| Directed assign 驗 cap | 是 — `assigned_to` 設了但 capability 不符 → 400 |
| `--model X` 衍生規則 | 內建 mapping（claude-opus/sonnet/haiku, gpt-4/4o/5, gemini, llama, mistral）。未知 model 只放 `model:X` |

### Sub-tasks

- [x] **C1.** Shared models 加 `required_all` / `required_any` + 建立 `shared/capabilities.py` matcher + derive_from_model
- [x] **C2.** DB schema migration（ALTER ADD COLUMN）+ `create_task` 帶新欄位 + `find_matching_agents` + `update_agent_capabilities`
- [x] **C3.** REST：`POST /tasks` 收新欄位 + `claim_task` 驗 cap（403）+ directed assign 驗 cap（400）+ `PATCH /agents/me` 收 capabilities
- [x] **C4.** WS hub：`emit_task_event(extra_targets=...)`，`POST /tasks` 計算匹配 online agent 當廣播對象
- [x] **C5.** MCP `create_task` tool 加 `required_all/required_any` 參數 + `mab-agent --model X / --capabilities a,b` flag + 啟動時 PATCH `/agents/me`
- [x] **C6.** 測試：matcher unit（10）+ derive_from_model unit + REST capability E2E（5）+ WS filter broadcast（1）
- [x] **C7.** TARGET.md 補 Phase 2.1 章節
- [x] **C8.** Fix WS heartbeat — broker 只在 `receive_text()` 更新 `last_heartbeat`，protocol ping 永遠不會觸發；client 加 app-layer text heartbeat
- [x] **C9.** Rewrite README — 寫入 Phase 1.5 deployment + 2.1 capability 完整設計，push 上 GitHub

---

## Phase 2.2 — Task delete + observability + live verification

- [x] **E1.** `DELETE /api/v1/tasks/{id}` — creator 或 assignee 可刪、任何狀態、自動清 `current_task`、廣播 `task_event:deleted`
- [x] **E2.** `tools/watch.py` — 連 broker 印 received messages / task events，用來看 capability filter broadcast 真的有過濾
- [x] **E3.** `task_event:deleted` 加進 protocol discriminator + MCP `delete_task` tool
- [x] **E4.** 升級 192.168.1.212 broker 到 Phase 2.1 + 2.2（含 schema migration ALTER ADD COLUMN）+ live-verify `PATCH /agents/me`、`required_all` 存取、claim 驗 cap（403）、heartbeat 真實更新
- [x] **E5.** Cross-machine multi-agent filter broadcast demo — claude-mac (opus) + worker-linux (sonnet) 兩 agent，3 個 case 驗證 broker 真的只 push 給匹配的 agent
- [x] **E6.** README 更新到含 Phase 2.2（DELETE / watch.py / 10 tools / 72 tests / cross-machine 已驗）

---

## Phase 3a — Lead Agent enabler (Roster + Dependencies + Channels)

目標：讓某個 agent 能扮演 lead role — 接 user 目標、拆 task、看 team 點名、指派、監督。Broker 維持 passive，只補 primitives；orchestration 邏輯放在 lead agent 的 prompt 裡。

### 決策（已鎖定）

| 項目 | 決定 |
|------|------|
| Orchestration 在哪 | Lead Agent 端，不是 broker 端 — 維持 LLM-agnostic |
| Roster 表現 | `AgentSnapshot` extends `Agent` 加 `last_heartbeat_age_seconds` + `is_stale` |
| Stale 閾值 | `heartbeat_interval * 3`（30s × 3 = 90s）。broker 不自動翻 status，只暴露讓 client 判斷 |
| `available_only` 語意 | `current_task is None` — 暴露給 lead 過濾「真的閒著的人」 |
| `current_task` 一致性 | 直接 `create_task(assigned_to=X)` 也設 X 的 `current_task`（跟 `claim_task` 對齊） |

### Roster (R)

- [x] **R1.** `AgentSnapshot` model + `db.find_matching_agents` 加 `available_only` 參數
- [x] **R2.** REST：`list_agents` / `get_me` / `get_agent` 回 `AgentSnapshot`；新 `POST /api/v1/agents/match`
- [x] **R3.** MCP `match_agents` tool + `BrokerClient.match_agents()`；`list_agents` tool docs 提 `is_stale`
- [x] **R4.** Tests：snapshot freshness、stale detection、match 過濾（caps / available_only / status）— 6 個新 test
- [x] **R5.** TARGET.md Phase 3a 章節

### Agent self-declaration gaps (G)

- [x] **G1.** `mab-agent` 啟動時 PATCH capabilities **before** WS connect，消除 broker `online` 廣播時 caps 過時的 race
- [x] **G2.** MCP tools `update_my_model` / `update_my_capabilities` — 讓 LLM 在 session 內自己改 declared model（例如 `/fast` 切 model 時）
- [x] **G3.** README 補非 MCP client 的寫法 + 「How an agent declares its model」段（4 條路徑說明）

### Slash-command mode skills (M)

- [x] **M1.** `.claude/skills/worker-mode/SKILL.md` — `/worker-mode` 把 session 切成 autonomous worker daemon（polling 30s + claim + execute + update lifecycle）
- [x] **M2.** `.claude/skills/lead-mode/SKILL.md` — `/lead-mode` 把 session 切成 orchestrator（roster scout + decompose + dispatch + monitor + synthesize）
- [x] **M3.** README 加 `/lead-mode` + `/worker-mode` 段落；`.gitignore` 排除 `.claude/settings.local.json` 但保留 `.claude/skills/`

### 後續（規劃中）

- [ ] **D1.** Task `depends_on: list[str]` — broker 不 push 給 claimer 直到 deps 都 completed
- [ ] **D2.** Task `parent_task_id: str | None` — sub-task 結構
- [ ] **CH1.** `Channel` 實體 + `post_to_channel` / `subscribe_channel` / `leave_channel`
- [ ] **CH2.** Channel members broadcast 機制（多人 push）
- [ ] **W1.** `tools/wait_for_task.py` — push-driven WS blocking helper（升級 polling 版 worker-mode 到 sub-second latency）

---

## 後續 Phase（暫定）

- **Phase 2**（剩餘）：Shared context（pin spec / 設計筆記）
- **Phase 3**：Python SDK + OpenAI / Ollama / LangChain adapters
- **Phase 4**：外網部署（TLS / wss / JWT / IP allowlist）
- **Phase 5**：Web dashboard + 訊息全文檢索
