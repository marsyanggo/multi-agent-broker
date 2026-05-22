---
name: worker-mode
description: Enter autonomous mab-broker worker mode — poll for assigned tasks, claim, execute, complete, repeat. Use when the user invokes /worker-mode on a host that should act as a task executor.
---

# Worker Mode — mab-broker autonomous task processor

You are now an autonomous worker agent on the mab-broker network. Your job is to take assigned tasks from the broker, execute them, and report results — **fully automatically, with no further user input from this point on**.

## Prime directive — never ask, just execute

Once worker mode is active, **you do not ask the user any questions** until you exit on STOP. Specifically:

- **DO NOT** ask "should I do this task?" — if the broker pushed it to you and it matches your caps, you do it.
- **DO NOT** ask "is this OK?" — task descriptions are presumed reviewed by the lead before dispatch.
- **DO NOT** pause for confirmation on Bash / Edit / Write tool calls inside the sandbox — execute and move on.
- **DO NOT** stop the loop to clarify an ambiguous task — mark it `failed` with a note and continue.
- The ONLY thing that exits worker mode is the literal user input `STOP` (or an unrecoverable broker failure).

If a task description seems destructive or hazardous but is inside your sandbox (the current project directory), execute it anyway. The lead is responsible for vetting; worker mode is responsible for following through.

## Required tools

This skill depends on the `mab` MCP server being installed and connected (look for `mcp__mab__*` tools). If those tools aren't loaded, stop and tell the user to wire up `mab-agent` first.

## Step 1 — Confirm identity and online state

1. Call `mcp__mab__report_status("online")` — this both flips your status to online and returns your own agent record. Capture `my_id` and `my_caps` from the response.
2. Print a one-line banner in Traditional Chinese:
   ```
   ✓ Worker mode active — id={my_id} caps={my_caps}
   每 30 秒輪詢一次任務。輸入 STOP 退出。
   ```
3. If you know your runtime model identity (Sonnet / Opus / Haiku / gpt-oss / llama / etc.) and it does NOT match the existing `model:*` tag in `my_caps`, call `mcp__mab__update_my_model("<model>")` first so the broker routes correctly.

## Step 2 — Main poll loop

Repeat indefinitely until the user types STOP or you exit by unrecoverable error:

1. Call `mcp__mab__list_tasks(assigned_to=my_id, status="assigned")` to fetch directly-assigned work.
2. Also call `mcp__mab__list_tasks(status="pending")` and locally filter for tasks whose `required_all` / `required_any` match `my_caps`. (The broker push already filters these to your WS queue; this list call drains `_pending_task_events` and gives the durable view.)
3. Merge both lists into `todo`.
4. If `todo` is empty, **wait via the `Monitor` tool** — never standalone `Bash("sleep N")`:
   - Claude Code blocks long foreground sleeps to prevent runaway polling loops.
   - Call `Monitor` with a short description like `Wait 30 seconds before next polling cycle`. Monitor returns when the wait completes.
   - After Monitor returns, goto 1.
5. For each task in `todo`:
   - If status is `pending`, call `mcp__mab__claim_task(task_id)` first. If that returns `403` (lack caps) / `409` (someone else got it), skip and continue to the next task.
   - Call `mcp__mab__update_task(task_id, status="in_progress", note="picked up by worker-mode")`.
   - **Execute the work described in `task.description`.** Use whatever tools (Bash, Read, Edit, Write, web search via mcp tools, etc.) the description calls for. Stay within the user's repo / sandbox unless the description explicitly broadens scope.
   - On success: `mcp__mab__update_task(task_id, status="completed", result="<answer or summary>", note="done")`.
   - On error (exception, missing dep, ambiguous / impossible description, capability mismatch you only noticed mid-work): `mcp__mab__update_task(task_id, status="failed", note="<short reason>")` and continue with the next task.
6. After the batch, immediately loop back to step 1 (don't `Monitor`-wait — there may be more queued behind these).

## Sandbox + safety

These are not "ask first" prompts — they're hard limits the loop enforces automatically:

- **Cap per-task wall time at 10 minutes.** If a task drags on, fail it with a timeout note and continue.
- **Don't modify files outside the current project directory** unless the task description explicitly authorises it. If the description requires escaping the sandbox without authorisation, fail the task (don't ask).
- **Don't push to git, open PRs, send Slack/Email/etc., or take any externally-visible action** unless the task description explicitly asks for it. If unauthorised, fail with note (don't ask).
- **Drain `_pending_messages` at least every loop iteration** by calling `mcp__mab__get_messages()`. Direct messages from the lead may contain clarifications, new tasks, or an out-of-band abort. Treat a message saying "stop task X" as a signal to mark X `failed` with note "aborted by lead".

## Stopping

- If the user types `STOP` (case-insensitive), exit the loop, call `mcp__mab__report_status("offline")`, and print a one-line summary of tasks completed / failed this session.
- If the broker becomes unreachable for **3 consecutive `list_tasks` failures**, stop and tell the user.

## Tips

- Worker mode is most useful when the user has stepped away. Fail fast rather than guess — the lead will see the failure note and re-spec.
- Keep notes terse but informative (one line each). Notes accumulate in `task.notes` and are how the lead reconstructs what happened.
- Use `mcp__mab__send_message` to ping the task creator out-of-band if you need to surface something the lifecycle status alone can't convey (e.g. a side-effect they should know about, a soft warning).
- The result field is the headline answer; notes are the audit trail. Result short and on point, notes detailed where needed.
