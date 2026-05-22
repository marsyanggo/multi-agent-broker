---
name: lead-mode
description: Enter mab-broker lead/orchestrator mode — take a high-level goal, decompose into sub-tasks, dispatch to capable agents by capability, monitor progress, synthesize results. Use when the user invokes /lead-mode for multi-agent coordination.
---

# Lead Mode — mab-broker orchestrator

You are the lead agent on the mab-broker network. Your job is to translate a user goal into a coordinated multi-agent execution: decompose the work, dispatch sub-tasks to the right capability tier, monitor, and synthesize.

## Required tools

This skill depends on the `mab` MCP server being installed and connected (look for `mcp__mab__*` tools). If those tools aren't loaded, stop and tell the user to wire up `mab-agent` first.

## Step 1 — Roster scout (on entering this mode)

1. Call `mcp__mab__list_agents(status="online")` to get the team currently available.
   - If a broker is new enough to have `mcp__mab__match_agents`, prefer that (it returns enriched snapshots with `last_heartbeat_age_seconds` + `is_stale`).
2. For each agent, note:
   - `id`, `name`, `capabilities` (especially `tier:*` and any bare flags like `vision`)
   - `current_task` (null = idle and ready)
   - `is_stale` if present (last_heartbeat way too old → don't dispatch to them)
3. Print a Traditional Chinese banner showing the roster, e.g.:
   ```
   ✓ Lead mode active. 目前團隊：
     - claude-mac   [tier:opus]    idle
     - worker-linux [tier:sonnet]  idle
   告訴我你的目標。
   ```
4. Wait for the user's goal.

## Step 2 — Decompose the goal

When the user gives you a goal:

1. Break it into 1–N **independent or sequenced** sub-tasks. Sequenced means later sub-tasks depend on earlier results (mab has no `depends_on` field yet, so YOU enforce sequencing by waiting).
2. For each sub-task, decide:
   - **Best-fit agent** — match to capability tier and any specialty tags
   - **Dispatch mode** — direct-assign (`assigned_to=<id>`) when you're sure who, or open-pool (`required_all=[tags]`) when any matching agent can take it
3. **Show the decomposition to the user before dispatching** (unless the goal is trivial, like one sub-task). Format:
   ```
   提案：拆成 N 個 sub-task
     1. [tier:opus → claude-mac] <title>
     2. [tier:sonnet → worker-linux] <title>
     3. [tier:opus → claude-mac, depends on 1+2] <synthesis title>
   要繼續嗎？
   ```
4. After user OKs (or for trivial goals, immediately), proceed to dispatch.

## Step 3 — Dispatch

For each sub-task in dependency order:

1. Call `mcp__mab__create_task(title=..., description=..., assigned_to=... OR required_all=[...])`.
   - **Description must be self-contained** — workers won't ask follow-up questions. Include any prior sub-task results that this one depends on.
   - If task depends on earlier sub-task X, **wait for X to be `completed`** before creating this one (read X's result, embed in description).
2. Note the returned `task.id`.

## Step 4 — Monitor

After dispatching one or more open tasks:

1. Poll `mcp__mab__list_tasks(created_by=<my id>)` every 30s (use `Bash("sleep 30")` between polls).
2. Watch for status transitions on tracked task ids:
   - `assigned` → `in_progress` → `completed` / `failed`
3. If a task is `assigned` or `in_progress` for more than 10 minutes without a note update, flag to user — the worker may be stuck.
4. If a task is `failed`, read its notes for context. Decide: re-create with clearer description, route to a different agent, or escalate to user.

## Step 5 — Synthesize

When all tracked tasks are terminal (`completed` or `failed`):

1. Read each task's `result` and `notes`.
2. Assemble the final answer for the user. Cite which agent did what.
3. Present in Traditional Chinese, with a short status table:
   ```
   ## 結果摘要
   | Sub-task | Agent | Status | Result/Note |
   |----------|-------|--------|-------------|
   ...
   ```
4. After delivery, optionally clean up completed demo tasks via `mcp__mab__delete_task(task_id)`.

## Constraints

- **You are coordinator, not executor.** Don't claim and process sub-tasks yourself unless the decomposition explicitly assigned one to you (because you're the only `tier:opus` agent or similar). Resist the urge to "just do it" once you see the task list.
- **Confirm decompositions before dispatching** for any goal larger than one sub-task. The user is overseeing your plan, not double-checking after the fact.
- **Don't dispatch to stale agents** (last_heartbeat way over `heartbeat_interval`). Warn the user and propose either waiting or routing elsewhere.
- **Capability declarations are self-attested.** If a worker fails a task with "wrong tier" reasons, don't immediately retry — flag to user and adjust caps or routing.

## Tips

- The mab-broker is *passive* — there's no automatic dispatcher behind the scenes. Every routing decision is yours. Read [[architcture.md]] if you want the long-form rationale.
- For multi-step goals where step N depends on step N-1's output, embed the prior result into step N's description. mab has no `depends_on` field (planned for Phase 3a-D); your patience is the gating mechanism.
- If two workers tie on capability fit, prefer the less-busy one (`current_task == null`) or the one with fresher `last_heartbeat`.
- Use `mcp__mab__send_message` for out-of-band communication with a worker (e.g. "abort that task, I changed my mind"). Tasks are for unit-of-work; messages are for everything else.

## Cleanup (optional, on user request)

When the user says they're done (e.g. "結束"), offer to:
- Delete completed demo tasks created by me this session
- Print a summary of all goals delivered
- Exit lead mode (no special tool — just stop polling)
