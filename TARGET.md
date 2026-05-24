# multi-agent-broker — Project Target

## 專案核心理念（thesis）

**讓不同 vendor 的 LLM 像一個 team 一起做事 — 以 capability 為契約，vendor 可換**。

單一 vendor 的 agent stack（Claude Code subagents / OpenAI Assistants / Gemini agents）已經把「同個 model N 個 instance 互相分工」做得很好 — 我們**不重做**那塊。mab-broker 是為了**跨 vendor / 跨硬體**這個更難的問題而存在的：

| 場景 | 用 broker 的理由 |
|------|----------------|
| Claude opus 規劃 → gpt-oss 推理 → Claude sonnet 收尾 | Anthropic 跟 OpenAI OSS 兩家不互通，broker 把它們變同 task pool |
| 本地 Llama-70b 處理 PII（不出網）+ 雲端 model 處理一般 task | 數據主權 + 跨網段，單 vendor 解不了 |
| Anthropic rate-limited → 自動 reroute 到 DeepSeek-R1 | Capability 是契約，vendor 是替換品 |
| GPU 主機跑 qwen-coder 寫 code + Claude sonnet review | 把每個 task 派給「最對的 LLM」，不是「最近的 LLM」 |

### 不適合 mab-broker 的場景（不要用）

- **單 vendor** — Claude Code 內建 subagent 已夠好，不需要 broker
- **N 個同 Claude 副本互動** — 用 Claude Code 的 `Task` / Agent tool 就好
- **同步對話 / 互動 chat** — broker 是 task-based，不是 turn-based
- **公網多租戶** — Phase 4 (TLS / JWT / IP allowlist) 還沒做，目前限 LAN / VPN / Tailscale

### 設計 invariants

1. **Capability 是 first-class** — task 寫 `required_all=["tier:reasoning"]`，不寫 agent_id。Lead 不需要知道有哪些 worker
2. **Vendor 可換** — 同 capability tag 可以由不同 vendor / 不同 host / 不同 model 提供，lead-side prompt 不變
3. **Broker passive** — broker 不做 orchestration 決策（不拆 plan、不選 LLM），那是 lead role 的工作。broker 只負責 message routing + capability filter
4. **每加一個 vendor = 一個 ~50 行 adapter**，不是 N 行的 client SDK + auth + retry boilerplate。`mab.worker.adapters.base.LLMAdapter` 就 3 個 abstract method

---

## Phase 1 範圍

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
- [x] **D7.** `deploy/setup-agent.sh` — 一鍵把 Claude Code 接上 broker（probe /health + 驗 key + `claude mcp add` 或 JSON 直編，含 backup）；install.sh 結尾 + deploy/README 補對應段落
- [x] **D8.** `setup-agent.sh --worker-host` flag — 同時寫 `<repo>/.claude/settings.local.json` 設 `defaultMode: bypassPermissions`，worker mode 全程不需 permission prompt
- [x] **D9.** `deploy/setup-worker.sh` — 一鍵裝 mab-worker daemon（probe + 寫 systemd unit + linger + enable + 等 online）；4 adapter 通吃；`--name` 支援多 worker per host

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
- [x] **G4.** Expand `derive_capabilities_from_model`：加 gpt-oss / llama-3 系列 / qwen / deepseek / phi / gemma / mistral 變體；`name:tag` Ollama 格式自動補 `size:` + `provider:ollama`
- [x] **G5.** Ollama Cloud `-cloud` 後綴解析：拆出 `size:<n>` + `host:cloud`（local Ollama 變 `host:local`），避免 `size:120b-cloud` 那個怪 tag

### Slash-command mode skills (M)

- [x] **M1.** `.claude/skills/worker-mode/SKILL.md` — `/worker-mode` 把 session 切成 autonomous worker daemon（polling 30s + claim + execute + update lifecycle）
- [x] **M2.** `.claude/skills/lead-mode/SKILL.md` — `/lead-mode` 把 session 切成 orchestrator（roster scout + decompose + dispatch + monitor + synthesize）
- [x] **M3.** README 加 `/lead-mode` + `/worker-mode` 段落；`.gitignore` 排除 `.claude/settings.local.json` 但保留 `.claude/skills/`

### 後續（規劃中）

- [ ] **D1.** Task `depends_on: list[str]` — broker 不 push 給 claimer 直到 deps 都 completed
- [ ] **D2.** Task `parent_task_id: str | None` — sub-task 結構
- [ ] **CH1.** `Channel` 實體 + `post_to_channel` / `subscribe_channel` / `leave_channel`
- [ ] **CH2.** Channel members broadcast 機制（多人 push）
- [x] **W1.** `wait_for_task` MCP tool — push-driven 取代 Monitor sleep；block 在 mab-agent 既有 WS queue 上，filter actionable tasks（pending OR assigned-to-me），sub-second latency。Skill 重寫 main loop 用 wait_for_task

---

## Phase 3 — Standalone worker daemon SDK

目標：擺脫「Claude Code session 當 worker」的 user-input race + monitor-sleep 脆弱性，用 standalone Python daemon 跑 production workers，supervised by systemd。Adapter 抽象支援 Anthropic / Ollama / Claude CLI 三條 backend。

### 決策（已鎖定）

| 項目 | 決定 |
|------|------|
| Worker 架構 | Path C — standalone daemon，不是 Claude Code session（user input + Monitor sleep 兩個脆弱點都不在 daemon path 上） |
| Adapter 抽象 | `LLMAdapter` abc，subclass `run_task(task) -> str`；optional setup/teardown |
| Adapter 依賴 | 零新外部 dep — Anthropic 跟 Ollama 都直接 httpx（既有），claude-cli 是 asyncio subprocess |
| Push-driven 機制 | 重用 Phase 3a 的 `wait_for_task` MCP primitive；daemon 直接呼叫 BrokerClient.pop_one_task_event |
| Event filter | `_task_event_queue` 改成 `(event_name, Task)` tuple；daemon 用 `only_events={"created"}` 過濾自己 claim/update echo |
| Per-task timeout | `asyncio.wait_for` 包 `adapter.run_task`，TimeoutError → mark failed with note |
| Process supervision | systemd `--user` service + linger，跟 mab-broker 對稱 |
| 多 worker per host | `--name <suffix>` 變成 `mab-worker-<suffix>.service`，env vars + journal 隔離 |

### Sub-tasks (F)

- [x] **F1.** Worker package skeleton + `LLMAdapter` ABC + `MockAdapter` + 11 unit tests _(commit `226be51`)_
- [x] **F2.** `AnthropicAdapter` — httpx direct to `/v1/messages`，支援 system prompt / temperature / multi-block concat + 8 tests with httpx.MockTransport _(commit `2df7084`)_
- [x] **F3.** `OllamaAdapter` — httpx direct to `/api/chat`，local + Ollama Cloud 通吃，bearer auth on cloud + 7 tests _(commit `541e0bd`)_
- [x] **F4.** `ClaudeCLIAdapter` — asyncio subprocess `claude -p`，prompt template、cancel handler kill subprocess、可選 `--no-skip-permissions` + 11 tests with Python shim binary _(commit `620964e`)_
- [x] **F5.** `WorkerDaemon` main loop — PATCH caps before WS connect、catch-up scan、push-driven via wait_for_task、per-task timeout、signal handler、error path mark failed not crash + 14 tests against live broker _(commit `28fd4ae`)_
- [x] **F6.** `mab-worker` CLI + pyproject script entry — per-adapter flag groups、env var defaults、required-flag validation + 13 tests _(commit `8a5da71`)_
- [x] **F7.** `deploy/setup-worker.sh` — 一鍵裝 daemon + 寫 systemd unit (chmod 600)、validate broker + key、poll for online、idempotent re-run、`--name` 多 worker per host _(commit `361d765`)_
- [x] **F8.** README + skill + TARGET 完整 update for Phase 3：3-role architecture diagram、Quickstart 多 daemon 區塊、roadmap tick、test layout 完整
- [x] **F9.** End-to-end demo tests — 4 個新 scenarios 覆蓋 unit tests 沒 cover 的路徑：cap-rejected claim、push-after-catchup（真正的 wait_for_task push path）、graceful stop mid-task、10-task burst throughput

### Production verification

- 2026-05-22 06:34 UTC: cross-machine demo 跑通。Mac claude-mac (Opus) curl `POST /tasks {required_all:[tier:reasoning, host:cloud]}` → broker push → 212 `mab-worker.service` (gpt-oss:120b-cloud via local Ollama → Ollama Cloud routing) → result `"daemon ok"`、notes `[picked up by worker daemon (ollama), done]`、**1.09 秒 end-to-end**（含 ~800ms gpt-oss inference + 100ms daemon pop + ~50ms broker round-trips）
- 對照之前 Claude Code `/worker-mode` push-driven 版的同樣 probe：6.05s
- 對照最早的 Monitor sleep 版：5+ 分鐘卡死

---

## Phase 3 — Task dependencies (depends_on)

目標：解 lead 拆 multi-step plan 必踩痛點 — 之前要 lead poll 中間結果再 craft 下一個 task description。`depends_on` 讓 lead **一次派完整個 plan**，broker 自動 gate downstream task 直到 upstream 完成。

### 決策（已鎖定）

| 項目 | 決定 |
|------|------|
| State model | 重用既有的 `status="blocked"` 表達「waiting on deps」 |
| Immutability | `depends_on` create-time set only — 不能 update 後改。Cycle 因此數學上不可能（不用 detection）|
| Unblock trigger | upstream `completed` → broker post-hook 掃 downstream blocked，全 deps 完成就 flip 成 pending + emit task_event:created 走原本 cap filter broadcast |
| Failure cascade | upstream `failed` 或 `delete` → downstream `failed` with note，**遞迴 propagate**（A→B→C 全 fail）|
| Result injection | worker 自己 fetch（broker 不轉 — 保持 passive bus）。task description 寫 "see result of task X"，worker 用 `get_task(X)` 拿 |
| Block-not-broadcast | task status="blocked" 時 broker **完全不 emit task_event:created**。Worker 看不到、claim 自然 reject |
| Claim 邏輯 | 既有 atomic SQL `WHERE status='pending'` 自動排除 blocked。Lead 無需改 claim 邏輯 |

### Sub-tasks (D)

- [x] **D1.** Task `depends_on: list[str]` + DB ALTER ADD COLUMN + create_task validation + initial status (blocked vs pending)
- [x] **D2.** Cycle detection — **moot, skipped**（depends_on immutable，cycle 數學上不可能）
- [x] **D3.** `_propagate_completion` hook in update_task — 全 deps 完成才 unblock + emit
- [x] **D4.** `_propagate_failure` hook — upstream fail/delete 遞迴 cascade downstream
- [x] **D5.** Tests — chain unblock、fan-in、3-step failure cascade、unknown dep reject、already-failed dep reject、already-completed dep skip-blocked
- [x] **D6.** MCP `create_task` tool + `BrokerClient.create_task` 加 `depends_on` 參數
- [x] **D7.** `/lead-mode` skill — 教 LLM 用 depends_on（fan-in vs sequential chain trade-off）
- [x] **D8.** TARGET / README 更新

### Production thesis

`depends_on` 讓 cross-vendor multi-step workflow 從「lead 端 手動 orchestration」變成「broker 自動 gate」。e.g.:

```python
plan = create_task("Outline strategy", required_all=["tier:opus"])           # Claude Opus
draft = create_task("Write 500 words based on plan {plan_id}",               # gpt-oss reasoning
                    required_all=["tier:reasoning"], depends_on=[plan])
review = create_task("Polish style + check facts for draft {draft_id}",      # Claude Sonnet
                     required_all=["tier:sonnet"], depends_on=[draft])
```

Lead 派完 3 個 task 就 done — broker 串行 gate、自動 cap routing、失敗自動 cascade。**這是真正讓 cross-vendor LLM workflow 可工程化的 piece**。

---

## Phase 3 — Lead-mode cookbook

`docs/cookbook.md` — production-verified cross-vendor recipes，每個都跑過實機。

- [x] Recipe 1 — Two-stage thinking (gpt-oss reasoning + claude-sonnet prose, sequential chain via depends_on)
- [x] Recipe 2 — Fan-in synthesis (two parallel inputs → one synthesiser，depends_on accepts multiple ids)
- [x] Recipe 3 — Failure cascade (upstream fail → downstream auto-fail with cascade note)
- [x] Troubleshooting table（schema migration trap、status flapping 等）
- [x] Recipe 4 — Shared context (pin spec / 上游 result auto-promote handoff)
- [x] Recipe 5 — Channels (group broadcast for coordination noise / open queries)
- [x] Recipe 6 — `/lead-mode` 端到端（natural language → fan-out + fan-in dino game demo, 4 tasks, ~5 min end-to-end, examples/dino.html artifact）
- [x] Recipe 7 — Adding a third vendor (Google Gemini worker on RPi) — adapter ~110 行 + CLI flag + capability derivation + setup-worker.sh，第 3 個 vendor 上線 capability filter 自動 routing。包含「gen-key 用 default DB vs broker MAB_DB_PATH 不一致」+ Gemini 429 billing 在 adapter 端 graceful fail 的 production lessons
- [x] 跑通 `/lead-mode` 端到端真實 demo（4-task 跨 vendor fan-in chain，B+C parallel ~25s，全鏈 ~5 min wall-clock）
- [x] 產出 `examples/dino.html` 可玩 artifact（gpt-oss IIFE + claude-sonnet HTML shell splice via `// GAME_LOGIC_HERE` placeholder，221 lines single-file，browser-verified）
- [x] README status header + roadmap 更新（Phase 3 fully complete 改寫，移除 "remaining" bullet）
- [x] Commit `ca4b52a` 推上 GitHub main（cookbook Recipe 6 + dino.html + README/TARGET）

---

## Phase 3 — Shared context (pinned reference docs)

`docs/cookbook.md` Recipe 4 文件。Persistent named documents any agent can read。解 cookbook recipe 1 點出的「downstream 沒 inline 拿到 upstream result」gap。

### 決策（已鎖定）

| 項目 | 決定 |
|------|------|
| Name unique? | 不唯一。reference 永遠 by id；`list_contexts(name="X")` 拿 latest 用 |
| WS broadcast for context events? | 沒有。Context 是 persistent reference 不是 event stream |
| Read auth | 任何已認證 agent 都能讀（share docs by design） |
| Mutate auth | 只 creator 可改 / 刪 |
| Task linkage | `task_id` 欄位選填，把 context 跟產生它的 task 連起來方便追溯 |
| Content type | reuse 既有 `ContentType` literal |

### Sub-tasks (S)

- [x] **S1.** `Context` pydantic model + DB schema + `db.create_context` / `get_context` / `list_contexts` / `update_context` / `delete_context`
- [x] **S2.** REST routes `POST/GET/PATCH/DELETE /api/v1/contexts`
- [x] **S3.** 9 REST 測試（basic create / task link / 404 / name filter / task_id filter / creator-only update / partial update / creator-only delete / any agent reads）
- [x] **S4.** MCP tools (`create_context` / `list_contexts` / `get_context` / `update_context` / `delete_context`) + BrokerClient methods
- [x] **S5.** 3 MCP 測試（round-trip / partial update / delete）
- [x] **S6.** Cookbook Recipe 4 — pin team-style-guide 模式 + auto-promote 上游 result 模式 + 未來 daemon expansion 註記

---

## Phase 3 — Channels (group broadcast)

`docs/cookbook.md` Recipe 5 文件。名稱唯一的 group-broadcast 頻道；member 才能 post，但 reading history 對所有 authenticated agent 開放。WS push 即時送給所有 online members。

### 決策（已鎖定）

| 項目 | 決定 |
|------|------|
| Name 唯一性 | 唯一。collision 預設 409，`auto_suffix=true` 自動加 `-2 / -3 / ...`（跟 gen-key 對齊） |
| 自動加入 creator | 是 — `create_channel` 自動把 creator 寫進 `channel_members` |
| Posting 權限 | 限 member（403 if not joined） |
| Reading history 權限 | 任何 authenticated agent 可讀（REST `GET /channels/{id}/messages`）|
| WS push | 發給所有 online members 包含 sender 自己（一致性、簡單）|
| Delete | creator 才能刪，cascade 清 members + messages |
| 跟 direct Message 關係 | 完全分開 table — channel_messages 帶 channel_id、沒 to_agent |

### Sub-tasks (CH)

- [x] **CH1.** `Channel` + `ChannelMessage` pydantic models + 3 tables (`channels`, `channel_members`, `channel_messages`) + db CRUD（create / get / list / delete / join / leave / list_members / is_member / post / list_messages）
- [x] **CH2.** REST routes `POST/GET/DELETE /api/v1/channels`、`POST /channels/{id}/join`、`POST /channels/{id}/leave`、`POST/GET /channels/{id}/messages` + `auto_suffix` 衝突處理
- [x] **CH3.** WS broadcast：新 `ChannelMessageEnvelope` 加進 protocol discriminator + `hub.emit_channel_message(msg, members)`
- [x] **CH4.** 11 個 REST + WS test (create / auto-join creator / duplicate-409 / auto-suffix / join+leave / my_membership filter / post-requires-member / post+list history / delete creator-only / WS broadcast to members / WS skip non-members)
- [x] **CH5.** MCP tools (`create_channel` / `list_channels` / `get_channel_info` / `join_channel` / `leave_channel` / `post_to_channel` / `get_channel_messages`) + BrokerClient methods + `_channel_message_queue` 加進 WS receive loop
- [x] **CH6.** `_with_pending` wrapper 加 `_pending_channel_messages` 計數 + 1 個 MCP lifecycle 整合測試（create → join → post → other member receives push → reads history）
- [x] **CH7.** Cookbook Recipe 5 + TARGET / README 收尾

### Production-grade primitive 對照

| Primitive | Shape | 用法 |
|-----------|-------|------|
| **Task** | 1 producer → N candidates → 1 claimer；lifecycle pending → assigned → completed | 「做這件事然後回報」 |
| **Direct message** | 1:1，持久 till pulled，offline backfill | 「告訴 agent X 這件事」 |
| **Channel message** | 1:N broadcast，ordered persistent log，real-time push | 「對 topic Y 有興趣的人都該看到。沒固定 claimer」 |
| **Context** | persistent named doc，任何 agent 可讀 | 「pin 一份 reference 給所有 task 用」 |

---

## Phase 3 — Reliability hardening (supersede race)

### 背景

兩個 mab-agent 共用同個 api-key 時（典型 case：上一個 Claude Code window 沒清乾淨就開新的）broker 1-WS-per-agent rule 用 close code 4002 "superseded" 把舊 WS 踢掉。原本兩個 bug 互相強化造成 reconnect storm：
- Broker 端 WS endpoint 的 finally clause 無條件 set status=offline，會蓋掉新 WS 才剛 set 的 online
- Client 端 `_run_ws_loop` 收到 4002 close 後一視同仁 backoff retry，再連回去又把對方踢掉，兩邊互踢無限循環

### Sub-tasks

- [x] Kill orphan mab-agent PID 46958（昨天 Claude window crash 留下的 20h zombie 是 reconnect storm 根因）
- [x] Broker fix — `WebSocketHub.disconnect()` 回 bool 表示自己是否仍是 active WS；endpoint finally 只在 `was_active=True` 時才 flip status=offline
- [x] Client fix — `BrokerClient._run_ws_loop` 偵測 `ConnectionClosed.rcvd.code == 4002` → log + `_stop_evt.set()` + break，不再 reconnect 製造 flap loop
- [x] 新 test `test_ws_supersede_keeps_status_online`（188 → 189 tests，全綠）
- [x] Commit `38e3869` 推上 GitHub main
- [x] 212 broker 升級 `./deploy/update.sh`（Phase 3 S/CH 後端 + supersede fix 一起 ship 上 production，broker openapi 9 → 16 paths）

---

## Phase 5a — Read-only web dashboard (MVP)

目標：把 broker 即時狀態（agents / tasks / channels / contexts）攤在一個瀏覽器頁面上，read-only。看 task graph (depends_on) 視覺化、看 agent 狀態圈跟著 WS push 即時變色。寫操作（dispatch/claim/delete）留到 5b，全文檢索留到 5c。

### 決策（已鎖定）

| 項目 | 決定 |
|------|------|
| Frontend stack | Vanilla HTML+JS+CSS — no build step、no framework、no `node_modules`。Task graph 用 SVG（5-50 node 不需要 d3）|
| 部署 | Bundled into broker — FastAPI 多掛 `/dashboard` static + 必要 admin routes，同 process / 同 port (8420) |
| Real-time | **MVP**: polling `GET /dashboard/snapshot` every 1s — simpler，避免改 broker envelope routing。**Enhancement (later)**: 加 admin WS broadcast 把 task_event/channel_message 也 push 給 `dashboard` pseudo-agent，砍掉 polling 過渡到 push-driven |
| Auth | Bearer token（同 mab-ak-* api key）— dashboard 開啟時用 query param 或 localStorage 帶 |
| 寫操作 | 5a 不做 — 純讀。所有 mutation 都是 5b 範圍 |

### Sub-tasks

格式：`owner` = 預計派給誰，`elapsed` = 從 in_progress → completed 的 wall-clock。`-` 表示尚未開始。

- [x] **5a-1.** Backend: FastAPI 掛 `/dashboard` static + `GET /api/v1/dashboard/snapshot` 一次拉全狀態（agents/tasks/channels-summary/contexts/server_time）+ 3 個 pytest（unauth / shape / populated state，192 tests 全綠）
      _owner: claude-mac (opus) | elapsed: ~10m_
- [x] **5a-2.** Backend: ~~dashboard pseudo-agent + admin WS~~ → **scope cut, deferred to enhancement**。MVP 用 1s polling /snapshot 取代，避免改 broker envelope routing。Frontend 用任何 valid api-key 走 REST 即可
      _owner: claude-mac (opus) | elapsed: 0m (deferred)_
- [x] **5a-3.** Frontend: HTML + CSS skeleton（2×2 panel grid + header bar，monospace + monochrome 配色跟 cookbook 同調）
      _owner: worker-claude-sonnet (sonnet) | elapsed: 2m18s | broker task `70d235ba`_
- [x] **5a-4.** Frontend: agents panel — `renderAgentsPanel(snapshot)` 渲染 status dot / name / tier tag / current_task / heartbeat freshness
      _owner: worker-claude-sonnet (sonnet) | elapsed: 13s | broker task `0d3eccdf` | quirk: 輸出含 ```javascript markdown fence（違反 contract），splice 時 strip 掉_
- [x] **5a-5.** Frontend: tasks panel — `renderTasksPanel(snapshot)` filter bar + task list + click-toggle detail panel
      _owner: worker-claude-sonnet (sonnet) | elapsed: 109s | broker task `32efc7a6`_
- [x] **5a-6.** Frontend: task graph SVG renderer — `renderTaskGraph(snapshot)` 層級拓樸 layout、SVG box+arrow、status color、hover tooltip
      _owner: worker-gpt-oss-cloud (reasoning) | elapsed: 20s | broker task `9b2e70b4`_
- [x] **5a-7.** Frontend: channels + contexts side panels — `renderChannelsPanel` + `renderContextsPanel` + relative-time helper，contexts 可點開 preview
      _owner: worker-claude-sonnet (sonnet) | elapsed: 67s | broker task `d53a4256` | quirk: 同 5a-4 markdown fence，strip 掉_
- [x] **5a-8.** Integration: 拼接 5a-3 HTML shell + 5a-4/5/6/7 JS modules + driver (api-key prompt + 1s poll loop)，strip fence quirk，寫到 `src/mab/broker/static/index.html`，212 deployed via `update.sh`，browser 驗證 live updates 端到端 OK
      _owner: claude-mac (opus) | elapsed: ~15m baseline + multiple visual polish rounds_
- [x] **5a-9.** Visual polish — 多輪 user feedback iteration: layout swap (tasks ↔ agents)、dark/tech theme (dark slate base + cyan accents)、top-to-bottom graph layout、graph box sizes (4 rounds 120→88→66→50→200)、layer horizontal centering、`min-width:0` flex fix for horizontal scroll、visible WebKit scrollbar、2-line title word-wrap、SVG natural-pixel rendering (no viewBox stretch)
      _owner: claude-mac (opus) | elapsed: ~1.5h across iterations_
- [x] **5a-10.** Batch grouping — graph 只顯示最新 connected component (via depends_on union-find)，新 dispatch 自動取代舊 batch；解「所有歷史 task 堆在一張圖」的問題
      _owner: claude-mac (opus) | elapsed: ~10m_
- [x] **5a-11.** Per-box meta strip — 每個 graph box 加 agent name (`worker-claude-sonnet`) + model (`claude-sonnet-4-6`) + live duration；duration 在 in_progress 狀態每秒 tick；BOX_H 48 → 64 給 meta 空間
      _owner: claude-mac (opus) | elapsed: ~10m + 1 iteration after user wanted "agent name+model" not just tier label_
- [x] **5a-12.** Pending → gray render — 「未跑的」(pending + blocked) 都灰色；color 只在開始跑時上 (gray → amber → green/red 三步生命週期)，配合 sentinel pattern 視覺乾淨
      _owner: claude-mac (opus) | elapsed: ~5m_
- [x] **5a-13.** Sentinel hold pattern — lead 端 trick：把 sentinel task 派給自己 + 整個 chain `depends_on=[sentinel]`，所有 node 都 blocked (灰) 一次畫好；user 說 go → lead complete sentinel → cascade unblock 真開跑。無 broker change，純 lead workflow 補位（broker 沒「draft」status，將來如果加 task pause/release 可廢除這個 hack）
      _owner: claude-mac (opus) | elapsed: ~5m_
- [x] **5a-14.** UX cleanup — `ongoing` → `active` filter rename (跟 `in_progress` status 重名混淆)；清掉 20h orphan "Push-driven probe" task (in_progress 卡住，舊 worker-mode session 寫 result="pong" 沒 mark completed 就掛)
      _owner: claude-mac (opus) | elapsed: ~5m_
- [x] **5a-15.** Demo screencast → GitHub release + issue + README — `demo-phase-5a-dashboard` release with `Demo_v1.mov` (988KB) 當 asset；`/issues/1` 嵌入 video + 對應 code path 解說；README 加 demo link；`demo/` 加 `.gitignore` 不入 git
      _owner: claude-mac (opus) | elapsed: ~10m_
- [x] **5a-16.** README inline-player upgrade — release asset URL 點下去是 download；改用 GitHub `user-attachments/assets/<uuid>` URL pattern（syncbench 同款）獨立一行擺著，自動 render 成 `<video>` player 點下去就播。User drag-drop video 到 issue#1 comment 取得 URL，README 加 `## Demo` section 收口
      _owner: claude-mac (opus) | elapsed: ~10m_

### 後續 increments（placeholder）

- **Phase 5b — Write actions** — dispatch task / claim / delete / send message / post channel msg 從 UI 操作
- **Phase 5c — Full-text search** — SQLite FTS5 index over messages + task descriptions/results + channel messages + contexts，dashboard 加 search bar

---

## Phase 5 — Broker reliability: worker-timeout reaper

### 背景

5a-14 暴露的問題：worker daemon 死了或失聯，broker 上的 task 永遠卡 `in_progress`/`assigned`。手動 delete 解決一次，但根因沒處理。`dashboard` active filter 越用越多 stuck task 累積；downstream `depends_on` chain 卡死沒人通知。

### 決策（已鎖定）

| 項目 | 決定 |
|------|------|
| 觸發條件 | task 在 `assigned`/`in_progress`，且 assignee 的 `last_heartbeat` 老於 stale threshold (default `heartbeat_interval × 3` = 90s) |
| 處理動作 | mark task `failed` + note "worker timeout: agent X stale Ys" + clear agent.current_task + emit task_event:failed + cascade failure 到 blocked downstream（重用既有 `_propagate_failure`） |
| Scan 頻率 | 預設每 60s 一輪（`MAB_TASK_REAP_INTERVAL_SECONDS`），跑在 broker app lifespan background task |
| Heartbeat-only rule | 不另外 check task age — agent fresh 但 task 久未 update 表示 worker 還活、可能在跑長 LLM call，不該被打死。Worker 端自己的 `asyncio.wait_for` 處理單 task timeout |
| Retry vs fail | 直接 `failed`，不 reset 成 `pending`。Lead 視情況手動 re-dispatch。Auto-retry 留到未來 |

### Sub-tasks (R)

- [x] **R1.** `src/mab/broker/reaper.py` — `reap_stuck_tasks()` 單次掃 + `reaper_loop()` 永動，loop 從 `app.py` lifespan 啟動
      _owner: claude-mac (opus) | elapsed: ~15m_
- [x] **R2.** Config: `task_reap_interval_seconds=60`, `task_reap_stale_multiplier=3` 加 `Settings`，可 `MAB_*` env 覆寫
      _owner: claude-mac (opus) | elapsed: ~2m_
- [x] **R3.** Tests — 4 個（stuck → failed / fresh agent → skip / cascade failure / ignore pending+completed），196 tests 全綠
      _owner: claude-mac (opus) | elapsed: ~10m_

### 之後可加

- 區分「worker timeout failure」跟「真實 task failure」的 metadata（讓 lead 知道值得 retry）
- Auto-retry mode：reaped task 不直接 failed，先嘗試 `pending` re-dispatch N 次
- Worker-side graceful shutdown hook：daemon 收 SIGTERM 時把 in-flight task `pending` 還回去而不是 fail
- Configurable per-task timeout override（some tasks expected to be very long）

---

## 後續 Phase（暫定）

- **Phase 4**：外網部署（TLS / wss / JWT / IP allowlist）— **暫時跳過，先做 Phase 5**
- **Phase 5b**：Web dashboard write actions
- **Phase 5c**：Full-text search
