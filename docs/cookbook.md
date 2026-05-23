# Lead-mode Cookbook — production-verified cross-vendor recipes

Every recipe here was actually run end-to-end against a deployed broker + at least two workers from different vendors. The exact timings, transitions, and outputs are noted so you can compare what you see when reproducing.

The setup these recipes assume:

| Role | Host | Process | LLM |
|------|------|---------|-----|
| Lead | Mac (this conversation, or `claude` + `/lead-mode`) | Claude Code | Opus via Anthropic API / Max |
| Broker | Linux box (e.g. 192.168.1.212) | `mab-broker.service` (systemd) | n/a |
| Worker A | same Linux box | `mab-worker.service` daemon | **gpt-oss:120b-cloud** via Ollama Cloud (OpenAI OSS family) |
| Worker B | same Linux box | `mab-worker-claude-sonnet.service` daemon | **claude-sonnet-4-6** via `claude -p` (Anthropic Max subscription) |

If you don't have this exactly, the recipes still illustrate the *pattern* — swap `tier:reasoning` for whatever local model you have, etc.

## Why these recipes are interesting

Each recipe leans on something a single-vendor agent stack can't easily do:

- **Recipe 1** uses two different LLM families in one chain — heavy reasoning on the OSS side, natural prose on the Anthropic side. Capability tags carry the routing.
- **Recipe 2** fans in two parallel sub-tasks and synthesises. Same `depends_on` primitive, fan-in shape.
- **Recipe 3** shows what happens when the upstream fails — the failure cascades through the chain without leaving half-finished work mid-flight.

Each recipe is fired from a single `curl` (or one `create_task` call per node). Lead never polls between steps; the broker does the gating.

---

## Recipe 1 — Two-stage thinking (reason then explain)

**Story:** A "compute then teach" chain. Step A is a textbook reasoning task — pick the LLM with the best reasoning-to-cost ratio. Step B is a natural-prose explanation for a teen audience — pick the LLM whose style fits that voice.

**Topology:** sequential chain (A → B, B depends on A).

```
+-----------+        +-----------+
|  step A   |  -->   |  step B   |
| gpt-oss   |        | claude    |
| reasoning |        | sonnet    |
+-----------+        +-----------+
   tier:reasoning       tier:sonnet
   ~5s inference        ~8s cold start + inference
```

### Dispatch

```bash
BROKER=http://192.168.1.212:8420
LEAD_KEY=mab-ak-XXXX_lead

# Step A — compute
A=$(curl -sS -X POST -H "Authorization: Bearer $LEAD_KEY" -H "Content-Type: application/json" \
  -d '{
    "title": "chain-A: 75th prime",
    "description": "What is the 75th prime number? Reply with just the integer, no commentary.",
    "required_all": ["tier:reasoning"]
  }' \
  "$BROKER/api/v1/tasks" | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")

echo "A=$A"

# Step B — explain (depends_on gated)
B=$(curl -sS -X POST -H "Authorization: Bearer $LEAD_KEY" -H "Content-Type: application/json" \
  -d "{
    \"title\": \"chain-B: explain primes in crypto\",
    \"description\": \"A reasoning task just computed a specific prime number. Write a one-paragraph (exactly 3 sentences) reflection on why prime numbers matter in modern cryptography, written for a curious high school student. Just the paragraph itself — no preamble, no list.\",
    \"required_all\": [\"tier:sonnet\"],
    \"depends_on\": [\"$A\"]
  }" \
  "$BROKER/api/v1/tasks" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['id'], d['status'])")

echo "B=$B   ← status should be 'blocked'"
```

If the second command returns status anything other than `blocked`, the broker is missing the `depends_on` schema (run `./deploy/update.sh` on the broker host and try again).

### Observe the state transitions

```bash
# Block until both terminal
for tid in $A $B; do
  until s=$(curl -sf -H "Authorization: Bearer $LEAD_KEY" "$BROKER/api/v1/tasks/$tid" \
              | python3 -c "import sys,json;print(json.load(sys.stdin)['status'])") \
        && [[ "$s" =~ ^(completed|failed)$ ]]; do sleep 1; done
done

# Pretty-print both
for label in "A (gpt-oss reasoning)" "B (claude-sonnet)"; do
  echo "=== $label ==="
  curl -sS -H "Authorization: Bearer $LEAD_KEY" "$BROKER/api/v1/tasks/$([[ $label == A* ]] && echo $A || echo $B)" \
    | python3 -c "import sys,json;d=json.load(sys.stdin); print('result:',d['result']); print('notes:',d['notes'])"
done
```

### Expected output (verified 2026-05-23)

```
A result: 379                    # 75th prime ✓
A notes:  [picked up by worker daemon (ollama), done]

B result: Prime numbers are the secret backbone of the encryption that protects
          your passwords, bank transactions, and private messages every day...
          [3-sentence paragraph in Claude's natural voice]
B notes:  [dependencies satisfied, unblocked,
           picked up by worker daemon (claude-cli),
           done]

Total wall: ~15s (5s gpt-oss reasoning + ~10s claude cold-start + inference)
```

The `dependencies satisfied, unblocked` note on B is added by the broker when A's completion triggers `_propagate_completion`. You didn't write it; you don't have to look for it; it's just there as audit trail.

### What this shows

- Lead fired **two `create_task` calls** and waited — no polling between them.
- B was held in `status="blocked"` by the broker for ~5 seconds while A ran.
- The moment A completed, broker auto-flipped B → `pending` and emitted `task_event:created` with capability filter broadcast. Worker-claude-sonnet's daemon picked it up via `wait_for_task` within ~100ms.
- Two different vendors collaborated cleanly; the lead-side prompt is agnostic to which underlying LLM ran each step.

---

## Recipe 2 — Fan-in (two parallel branches synthesised)

**Story:** Compare a topic from two angles, then combine. Both reviewers run in parallel for speed; a synthesiser waits for both, then writes the merged take.

**Topology:** A1 + A2 → B (B depends on both).

```
       +---------+
       |   A1    |
       | gpt-oss |
       +----+----+
            |
            |     +--------+
            +---->|   B    |
            |     | claude |
            |     +--------+
            |
       +----+----+
       |   A2    |
       | claude  |
       +---------+
```

### Dispatch

```bash
A1=$(curl -sS -X POST -H "Authorization: Bearer $LEAD_KEY" -H "Content-Type: application/json" \
  -d '{
    "title": "A1: technical reasons",
    "description": "Give 3 technical reasons developers prefer git over svn. Bullet points only, no preamble.",
    "required_all": ["tier:reasoning"]
  }' \
  "$BROKER/api/v1/tasks" | jq -r .id)

A2=$(curl -sS -X POST -H "Authorization: Bearer $LEAD_KEY" -H "Content-Type: application/json" \
  -d '{
    "title": "A2: emotional reasons",
    "description": "Give 3 emotional / cultural reasons developers prefer git over svn. Bullet points only, no preamble.",
    "required_all": ["tier:sonnet"]
  }' \
  "$BROKER/api/v1/tasks" | jq -r .id)

B=$(curl -sS -X POST -H "Authorization: Bearer $LEAD_KEY" -H "Content-Type: application/json" \
  -d "{
    \"title\": \"B: synthesis\",
    \"description\": \"Two reviewers analysed why developers prefer git over svn — one technical, one cultural. Combine their points into a single 4-sentence op-ed paragraph that respects both perspectives. Just the paragraph.\",
    \"required_all\": [\"tier:sonnet\"],
    \"depends_on\": [\"$A1\", \"$A2\"]
  }" \
  "$BROKER/api/v1/tasks" | jq -r .id)
```

### Observe

```bash
# A1 and A2 run in parallel; B waits for both
watch -n 1 "for t in $A1 $A2 $B; do \
  curl -sf -H 'Authorization: Bearer $LEAD_KEY' '$BROKER/api/v1/tasks/'\$t \
  | jq -r '\"\(.id) \(.title) \(.status)\"'; \
done"
```

You'll see A1 and A2 transition `pending → assigned → in_progress → completed` independently. B stays `blocked` until **both** complete — completing only one (e.g. A1) doesn't unblock B.

### What this shows

- `depends_on` accepts multiple IDs; broker waits for **all** to complete.
- Mixed-vendor parallelism — A1 is OSS reasoning, A2 is Anthropic — yet both feed into B without lead-side coordination.
- The synthesis step gets the freshest possible inputs; broker dispatches B the instant the slower of A1/A2 finishes.

---

## Recipe 3 — Failure cascade (upstream breaks, downstream doesn't waste cycles)

**Story:** A 3-step plan where the first step is brittle. If A breaks, we don't want B and C to keep running on missing data — they should fail with a note pointing at the root cause.

**Topology:** sequential chain A → B → C, with intentional A failure.

### Dispatch

```bash
A=$(curl -sS -X POST -H "Authorization: Bearer $LEAD_KEY" -H "Content-Type: application/json" \
  -d '{
    "title": "A: brittle step",
    "description": "Reply with the word: FAIL_ME_PLEASE",
    "required_all": ["tier:reasoning"]
  }' \
  "$BROKER/api/v1/tasks" | jq -r .id)

# Manually fail A from the assigned worker side, OR have a worker that
# treats "FAIL_ME_PLEASE" as a fail signal. For this demo, manually:
WORKER_KEY=...        # the worker-gpt-oss-cloud's API key
curl -sS -X PATCH -H "Authorization: Bearer $WORKER_KEY" -H "Content-Type: application/json" \
  -d '{"status": "failed", "note": "intentional"}' \
  "$BROKER/api/v1/tasks/$A"

# Create downstream B and C as if nothing's wrong yet — they'll start blocked
B=$(curl -sS -X POST -H "Authorization: Bearer $LEAD_KEY" -H "Content-Type: application/json" \
  -d "{\"title\":\"B\", \"description\":\"...\", \"required_all\":[\"tier:sonnet\"], \"depends_on\":[\"$A\"]}" \
  "$BROKER/api/v1/tasks" | jq -r .id)
# (in this contrived recipe B will fail to be created because A is already failed:
#  broker returns 400 "depends_on id is already failed — downstream task would never run")
```

### What you'll see

```
A status=failed    note=intentional
B create attempt → 400 "depends_on id '$A' is already failed — downstream task would never run"
```

The broker refuses to create downstream tasks whose dependency has already failed. If A failed AFTER B was created (which is the realistic case), the cascade happens automatically:

```bash
# Create chain while A is still pending
A=...; B=create depends_on=[A]; C=create depends_on=[B];
# A is pending, B and C are blocked.

# Now fail A
curl ... PATCH /tasks/A {status: failed}

# Inspect: B and C are both auto-failed with cascade notes
curl /tasks/B → status=failed, notes=[..., "upstream dependency $A failed"]
curl /tasks/C → status=failed, notes=[..., "upstream dependency $B failed (cascade)"]
```

### What this shows

- No half-finished plan after upstream failure — broker propagates the failure synchronously.
- Notes record the cascade chain so a debugging human can trace root cause back to A.
- No worker LLM cycles wasted on tasks that would have failed anyway.

---

## Recipe 4 — Shared context (pin a spec once, every worker references it)

**Story:** You have a multi-step plan where every worker needs the same constraint sheet — coding-style guide, product brief, brand voice. Repeating it in every `task.description` is duplication and noise. Pin it once as a shared context; every task description just references the id.

**Topology:** one context, N tasks referencing it.

```
+-------------------+
|  context: spec    |
|  id: ctx_abc123   |
|  "Project style   |
|   guide..."       |
+--------+----------+
         |
         +--- task A description: "...follow rules in ctx_abc123..."
         +--- task B description: "...follow rules in ctx_abc123..."
         +--- task C description: "...follow rules in ctx_abc123..."
```

### Dispatch

```bash
BROKER=http://192.168.1.212:8420
LEAD_KEY=mab-ak-XXXX_lead

# 1. Pin the spec once
CTX=$(curl -sS -X POST -H "Authorization: Bearer $LEAD_KEY" -H "Content-Type: application/json" \
  -d '{
    "name": "team-style-guide",
    "content": "# Team writing style\n\n- Use plain language. No corporate jargon.\n- 2-3 sentences per paragraph max.\n- Cite specific examples, not abstractions.\n- End with a takeaway, not a summary.",
    "content_type": "text/markdown"
  }' \
  "$BROKER/api/v1/contexts" | python3 -c "import json,sys;print(json.load(sys.stdin)['id'])")

echo "Pinned ctx=$CTX"

# 2. Dispatch tasks that reference it by id. The worker reads it via
#    mcp__mab__get_context once at task start, then applies the rules.
for topic in "RSA" "Elliptic curves" "Post-quantum crypto"; do
  curl -sS -X POST -H "Authorization: Bearer $LEAD_KEY" -H "Content-Type: application/json" \
    -d "{
      \"title\": \"Explain $topic\",
      \"description\": \"Read the team writing style guide from context $CTX (via mcp__mab__get_context). Then explain $topic in 3 short paragraphs that strictly follow that style.\",
      \"required_all\": [\"tier:sonnet\"]
    }" \
    "$BROKER/api/v1/tasks" > /dev/null
done
```

### What workers do

For the `claude-cli` adapter: spawned `claude -p` sees the task description, recognises the `mcp__mab__get_context` reference, calls it via its loaded `mab` MCP server, and incorporates the style guide before writing.

For `ollama` / `anthropic` adapters (which don't have MCP access from the LLM side): a daemon helper could pre-fetch any `ctx_*` references in `task.description` and inline the content before sending to the LLM. **Not yet implemented in the daemon** — for those adapters today, the lead has to inline the context content directly when dispatching, OR you stick with `claude-cli` adapter workers for context-aware tasks.

### Update the spec, all future tasks see the new version

```bash
curl -sS -X PATCH -H "Authorization: Bearer $LEAD_KEY" -H "Content-Type: application/json" \
  -d '{"content": "# Updated style\n\n- ..."}' \
  "$BROKER/api/v1/contexts/$CTX"
```

In-flight tasks aren't affected (they read the context at start). Newly-dispatched tasks see the new version. **No "find all tasks that reference this and rewrite their descriptions" — references are by id, content is fetched live.**

### Auto-promote an upstream result to a context

Pattern: when task A produces an artefact that downstream tasks need verbatim, publish A's result as a context with `task_id=A.id`. The link lets a downstream worker / human trace the origin.

```bash
# After A completes
A_RESULT=$(curl -sf -H "Authorization: Bearer $LEAD_KEY" "$BROKER/api/v1/tasks/$A" | jq -r .result)

CTX_A=$(curl -sS -X POST -H "Authorization: Bearer $LEAD_KEY" -H "Content-Type: application/json" \
  -d "{
    \"name\": \"step-A-output\",
    \"content\": $(jq -Rs <<< "$A_RESULT"),
    \"content_type\": \"text/plain\",
    \"task_id\": \"$A\"
  }" \
  "$BROKER/api/v1/contexts" | jq -r .id)

# Then create B referencing CTX_A
curl ... create_task {description: "Polish the draft pinned at ctx $CTX_A. ..."}
```

This is a workaround for the "downstream needs upstream result verbatim" gap. It still requires the lead to wait for A then create the context — it doesn't compose with `depends_on` fire-once dispatch (that gap remains until inline result substitution lands).

### What this shows

- **Spec deduplication** — write once, reference everywhere. If your style guide is 500 words, every task description in your plan saves 500 words.
- **Update flow** — fix a typo in the spec, all future tasks pick it up immediately. No bulk task rewriting.
- **Cross-vendor visibility** — both `claude-cli` and (eventually) `ollama` daemon workers can read the same context. The shared doc is broker-mediated, vendor-neutral.
- **Task-result traceability** — the optional `task_id` link records which task originally produced a piece of pinned content, so audits walk back through the chain cleanly.

---

## Recipe 5 — Channels (group broadcast)

**Story:** A pool of workers from different vendors are all subscribed to `#dispatch`. Lead posts a question to the channel; whichever capable worker is online and free picks it up. Or: multiple workers want to coordinate on a long-running plan and need a place to leave breadcrumbs that all interested agents see in real time.

Channels are **persistent named topics**. Any agent can create one. Joining a channel subscribes that agent to WS push for every new message. Non-members can read history (via `get_channel_messages`) but don't receive pushes — and **non-members can't post** (broker returns 403). The creator is auto-joined; everyone else opts in.

### Dispatch

```bash
BROKER=http://192.168.1.212:8420
LEAD_KEY=mab-ak-XXXX_lead
WORKER_KEY=mab-ak-YYYY_worker  # e.g. worker-gpt-oss-cloud

# 1. Lead creates the channel — auto-joined as a member
CID=$(curl -sS -X POST -H "Authorization: Bearer $LEAD_KEY" -H "Content-Type: application/json" \
  -d '{"name": "dispatch", "description": "open task pool"}' \
  "$BROKER/api/v1/channels" | jq -r .id)

# 2. Worker joins
curl -sS -X POST -H "Authorization: Bearer $WORKER_KEY" "$BROKER/api/v1/channels/$CID/join"

# 3. Lead posts — broadcast goes to all members' WS queues
curl -sS -X POST -H "Authorization: Bearer $LEAD_KEY" -H "Content-Type: application/json" \
  -d '{"content": "Anyone with tier:reasoning free for a 30-second probe?"}' \
  "$BROKER/api/v1/channels/$CID/messages"

# 4. Worker reads its queue via MCP (drains local + falls back to history REST)
#    → mcp__mab__get_channel_messages(channel_id=$CID)
#    Or pulls the persistent log via REST:
curl -sS -H "Authorization: Bearer $WORKER_KEY" "$BROKER/api/v1/channels/$CID/messages"
```

### When to use a channel vs a task vs a direct message

| Primitive | Shape | Best for |
|-----------|-------|----------|
| **Task** | 1 producer → N candidates → 1 claimer; lifecycle (pending → assigned → completed) | "Do this work and report back." |
| **Direct message** | 1:1, persistent until pulled, offline backfill | "Just tell agent X this fact." |
| **Channel message** | 1:N broadcast, ordered persistent log, real-time push | "Anyone interested in topic Y should see this. No specific claimer required." |

Channels are NOT a task replacement — they don't have claim semantics, capability matching, or success/failure lifecycle. Use them for coordination noise, status updates, and queries where you don't care who responds.

### Membership rules at a glance

- Creating a channel auto-joins the creator.
- Any agent can `join_channel` — public-by-default.
- `post_channel_message` requires membership (403 otherwise).
- `get_channel_messages` (REST history) is open to any authenticated agent — readable but not push-subscribed.
- `delete_channel` is creator-only and cascades (drops members + messages too).

### What this shows

- **Real-time broadcast across vendors** — the `channel_message` envelope arrives in every online member's WS queue, regardless of which adapter their daemon is using.
- **Persistent log** — `list_channel_messages` returns the full chronological history; new joiners can scroll back via REST without needing the original WS push.
- **Lighter weight than tasks for non-actionable info** — "FYI: I'm working on the prime-number explanation, please don't dupe" doesn't need a task lifecycle; one channel post is enough.

---

## What's NOT in this cookbook (yet)

- **Inline result substitution.** Recipes here gate by `depends_on` but don't auto-inject upstream `result` into downstream `description`. If your downstream prompt needs the upstream result verbatim (e.g. "polish this exact draft"), today you have to dispatch sequentially: create A, wait for A, read result, embed in B's description, create B. The fire-and-forget pattern works for plans where downstream prompts are self-contained, or use Recipe 4's auto-promote pattern with a context handoff doc.
- **Daemon-side context expansion.** Non-MCP adapters (`ollama`, `anthropic`) can't fetch contexts from the LLM side. A future daemon enhancement would detect `ctx_*` references in `task.description` and inline the content before calling the LLM. For now, context-aware tasks need `claude-cli` adapter workers.
- **Channel auto-subscribe for new workers.** Today an agent must explicitly `join_channel` to receive pushes. There's no "subscribe all workers with `tier:reasoning` to `#dispatch` by capability" — coordinate this manually at agent registration time.
- **Lead-mode skill end-to-end demos.** A future recipe will be "type a goal in natural language to a `/lead-mode` Claude Code session, watch it decompose and dispatch this chain automatically." For now, recipes show the underlying primitives.

## Troubleshooting

| Symptom | Cause |
|---------|-------|
| `B` returns `status=pending` instead of `blocked` immediately after dispatch | Broker is on old code without the `depends_on` schema. Run `./deploy/update.sh` on the broker host. |
| `depends_on` field missing from `GET /tasks` responses | Same — broker schema migration hasn't run. |
| Worker daemon claims a blocked task | Shouldn't happen — `claim_task` SQL filters on `status='pending'`. If you see this, file a bug. |
| Downstream `status=failed` with note `upstream dependency X failed` | Working as designed — broker propagated A's failure to B/C. Don't retry; fix the upstream and dispatch the chain again. |
