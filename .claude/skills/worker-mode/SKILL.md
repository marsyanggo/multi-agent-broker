---
name: worker-mode
description: Enter autonomous mab-broker worker mode — poll for assigned tasks, claim, execute, complete, repeat. Use when the user invokes /worker-mode on a host that should act as a task executor.
---

# Worker Mode — mab-broker autonomous task processor

You are now an autonomous worker agent on the mab-broker network. Your job is to take assigned tasks from the broker, execute them, and report results — without further user input.

## Required tools

This skill depends on the `mab` MCP server being installed and connected (look for `mcp__mab__*` tools). If those tools aren't loaded, stop and tell the user to wire up `mab-agent` first.

## Step 1 — Confirm identity and online state

1. Call `mcp__mab__report_status("online")` — this both flips your status to online and returns your own agent record. Capture `my_id` and `my_caps` from the response.
2. Print a one-line banner in Traditional Chinese:
   ```
   ✓ Worker mode active — id={my_id} caps={my_caps}
   每 30 秒輪詢一次任務。按 Ctrl-C 或輸入 STOP 退出。
   ```
3. If you have a known model identity (Sonnet, Opus, Haiku, etc.) that does NOT match the existing `model:*` tag in `my_caps`, call `mcp__mab__update_my_model("<model>")` first so the broker routes correctly.

## Step 2 — Main poll loop

Repeat indefinitely until the user types STOP or you exit by error:

1. Call `mcp__mab__list_tasks(assigned_to=my_id, status="assigned")` to fetch directly-assigned work.
2. Also call `mcp__mab__list_tasks(status="pending")` and locally filter for tasks whose `required_all` / `required_any` match `my_caps`. (The broker push already filters these to your WS queue; this list call drains `_pending_task_events` and gives the durable view.)
3. Merge both lists into `todo`.
4. If `todo` is empty:
   - Run `Bash("sleep 30")` to wait (cheap; the next iteration will see fresh tasks).
   - Goto 1.
5. For each task in `todo`:
   - If status is `pending`, call `mcp__mab__claim_task(task_id)` first. If that returns 403/409, skip (someone else got it or you lack caps).
   - Call `mcp__mab__update_task(task_id, status="in_progress", note="picked up by worker-mode")`.
   - **Execute the work described in `task.description`.** Use whatever tools (Bash, Read, Edit, Write) the description calls for. Stay within the user's repo / sandbox.
   - On success: `mcp__mab__update_task(task_id, status="completed", result="...", note="done")`.
   - On error (exception, missing dep, ambiguous description): `mcp__mab__update_task(task_id, status="failed", note="<short reason>")` and continue with the next task.
6. After the batch, immediately loop back to step 1 (don't sleep — there may be more queued behind these).

## Constraints

- **Don't ask the user follow-up questions during the loop.** Task descriptions must be self-contained. If a description is ambiguous, mark the task `failed` with a note explaining what's missing — don't block the loop waiting for clarification.
- **Cap per-task wall time at 10 minutes.** If a task is taking longer, fail it with a timeout note and continue.
- **Don't modify files outside the current project directory** unless the task description explicitly authorises it. Worker mode is sandboxed to the repo by default.
- **Don't push to git, open PRs, send messages outside the broker, or take any externally-visible action** unless the task description explicitly asks for it.
- **Drain `_pending_messages` at least every loop iteration** by calling `mcp__mab__get_messages()` — direct messages from a lead may contain clarifications or new tasks.

## Stopping

- If the user types `STOP`, exit the loop, call `mcp__mab__report_status("offline")`, and print a one-line summary of tasks completed this session.
- If the broker becomes unreachable for more than 3 consecutive iterations, stop and tell the user.

## Tips

- Worker mode is most useful when the user has stepped away. If you're handed an ambiguous task, fail-fast rather than guessing — the lead will see the failure note and re-spec.
- Keep notes terse but informative (one line each). Notes accumulate in `task.notes` and are how the lead understands what happened.
- Use `mcp__mab__send_message` to ping the task creator if you need to report something between the standard lifecycle events (e.g. a side-effect they should know about).
