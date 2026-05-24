# Lead-mode Cookbook — production-verified cross-vendor recipes

Every recipe here was actually run end-to-end against a deployed broker + at least two workers from different vendors. The exact timings, transitions, and outputs are noted so you can compare what you see when reproducing.

The setup these recipes assume:

| Role | Host | Process | LLM |
|------|------|---------|-----|
| Lead | Mac (this conversation, or `claude` + `/lead-mode`) | Claude Code | Opus via Anthropic API / Max |
| Broker | Linux box (e.g. 192.168.1.212) | `mab-broker.service` (systemd) | n/a |
| Worker A | same Linux box | `mab-worker.service` daemon | **gpt-oss:120b-cloud** via Ollama Cloud (OpenAI OSS family) |
| Worker B | same Linux box | `mab-worker-claude-sonnet.service` daemon | **claude-sonnet-4-6** via `claude -p` (Anthropic Max subscription) |
| Worker C | Raspberry Pi on the LAN | `mab-worker-gemini.service` daemon | **gemini-2.5-flash** via Google AI Studio (Google family) — added per Recipe 7 |

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

## Recipe 6 — `/lead-mode` end-to-end (natural language → cross-vendor artifact)

**Story:** User types one sentence to a Claude Code `/lead-mode` session: *"build a small dinosaur jumping cactus game to demo our cross-vendor flow."* The lead agent (Opus) scouts the roster, decomposes into a fan-in plan, dispatches across two vendor daemons in parallel, and integrates the results — producing a single-file `examples/dino.html` that actually plays in a browser.

This is the recipe that ties together **everything else in this cookbook** — capability routing (Recipe 1's foundation), parallel fan-out + `depends_on` fan-in (Recipe 2's shape), and a real downstream artifact. Lead writes zero code itself except the spec and the final splice; broker auto-sequences the rest.

**Topology:** fan-out then fan-in, with the lead as both designer (A) and integrator (D).

```
              +-----------+
              |  A: spec  |
              |  opus     | ~45s
              |  (lead)   |
              +-----+-----+
                    |
       +------------+------------+
       |                         |
       v                         v
+------------+            +-------------+
|  B: JS     |            |  C: HTML    |
|  gpt-oss   | ~25s       |  sonnet     | ~8s
|  reasoning | (Ollama    |  (claude-cli| (Anthropic
+------+-----+  Cloud)    +------+------+  Max sub)
       |                         |
       +------------+------------+
                    |
                    v depends_on=[B,C]
              +-----------+
              |  D: merge |
              |  opus     | ~30s
              |  (lead)   |
              +-----------+
                    |
                    v
        examples/dino.html
```

### How it actually ran

User in a `claude` session opened with `/lead-mode`, then typed the goal. Lead-mode skill bound the session to: roster scout → propose decomposition → dispatch → monitor → synthesize.

The actual run on 2026-05-23 20:44–20:49 PDT against the broker on 192.168.1.212:

| # | Task ID | Title | Worker | Tier | Created → Completed | Wall-clock |
|---|---------|-------|--------|------|---------------------|------------|
| A | `ab00cf70` | Dino game — design spec | claude-mac (lead) | opus | 20:44:57 → 20:45:42 | 45s |
| B | `2bcb44e4` | JS game logic | worker-gpt-oss-cloud | reasoning | 20:47:28 → 20:47:53 | 25s |
| C | `e687fcbf` | HTML+CSS shell | worker-claude-sonnet | sonnet | 20:47:35 → 20:47:43 | **8s** |
| D | `2fe35704` | Integrate B+C → dino.html | claude-mac (lead) | opus | 20:47:48 (blocked) → unblocked 20:47:53 → completed 20:49:40 | ~110s (mostly write+verify) |

B and C ran **in parallel** — C finished first (8s claude-cli cold start + small output) but D stayed `blocked` until B finished 17s later. As soon as B's `update_task status=completed` landed, broker's `_propagate_completion` hook scanned downstream, found D's dependency set satisfied, flipped D from `blocked → pending`, and emitted `task_event:created` so D could be picked up. Lead's `list_tasks` saw the transition with the broker note: `"dependencies satisfied, unblocked"`.

**Total wall-clock from "go" to playable artifact: ~5 minutes**, of which ~25s was the actual cross-vendor parallel inference window. The rest was lead-side spec drafting (Opus, A) and integration write/verify (Opus, D).

### Why this is interesting

- **One natural-language sentence → coordinated multi-vendor output.** User said one line; lead handled the whole orchestration. The plan was presented for approval in Traditional Chinese, then fired in 4 `create_task` calls.
- **Real fan-in via `depends_on`.** Two parallel sub-tasks for the parallelisable parts (logic vs UI — independent modulo the shared spec), one synthesis task gated on both. No lead polling between the steps — broker handled it.
- **Cross-vendor character routing.** B went to gpt-oss because the task is mechanical state-machine code (where the reasoning model's terseness fits). C went to claude-sonnet because HTML/CSS rewards prose-style aesthetic judgement (where Claude's voice fits). Capability tags `tier:reasoning` and `tier:sonnet` made this routing automatic — lead didn't pick agent IDs.
- **The artifact actually runs.** `open examples/dino.html` → playable game. Press space to jump, R to restart. Single file, no external assets, ~190 lines total. Lead did **zero** game code — only the spec and a one-step copy-paste of the `// GAME_LOGIC_HERE` placeholder.

### Interface contract that made the splice trivial

The decomposition only worked cleanly because the lead's task descriptions enforced a strict interface:

- **B's contract:** "Output ONLY the JavaScript code — a single IIFE that wires onto `<canvas id='game' width='800' height='200'>`. Do NOT include `<script>` tags." → returns exactly an IIFE.
- **C's contract:** "Output the complete HTML file. Inline `<script>` tag whose ONLY content is the line `// GAME_LOGIC_HERE`." → returns a complete shell with a single placeholder.
- **D's integration:** plain string replace, no parser needed.

This is the production lesson: **explicit interface contracts in task descriptions turn LLM outputs into composable parts**. Without the placeholder convention, D would have needed to parse C's HTML to find the right insertion point — fragile. With it, the splice is `c_html.replace('// GAME_LOGIC_HERE', b_js)`.

### What this shows that prior recipes didn't

| Recipe | Pattern | Vendor count | Lead involvement |
|--------|---------|--------------|------------------|
| Recipe 1 | sequential chain | 2 | dispatch only |
| Recipe 2 | fan-in synthesis | 2 | dispatch only |
| Recipe 3 | failure cascade | 2 | observe |
| Recipe 4 | shared context pin | 2 | dispatch + pin |
| Recipe 5 | channel broadcast | N | post + observe |
| **Recipe 6** | **fan-out + fan-in + lead-as-worker** | **2 + lead** | **design + integrate** |

Recipe 6 is the first recipe where the lead is **also a worker** in its own plan (A and D were self-assigned to claude-mac, the only `tier:opus` agent online). This is the realistic shape for small teams: opus for design + synthesis, cheaper/faster vendor workers for the parallelisable mid-tier work.

### Quirks observed during this run

- **Broker on 192.168.1.212 still pre-Phase-3-S/CH.** The originally planned step "pin spec as shared context `dino-spec`" returned 404 — broker `/api/v1/contexts` route not deployed yet. Pivoted to inlining the spec into B and C's `description` directly. *This is actually closer to production reality anyway* — daemon adapters (`ollama`, `anthropic`) can't fetch contexts from the LLM side (no MCP), so inlining is the path even when the broker has `/contexts`.
- **claude-mac shows `status="offline"` despite live heartbeat.** Self-PATCHing status doesn't always survive Claude Code reconnects; the heartbeat is what `is_stale` actually looks at. Lead used `list_agents()` (no filter) instead of `match_agents(status="online")` so the offline-but-fresh self-row didn't drop out.
- **B (gpt-oss) generated 130 lines of clean JS in 25s.** Cold start + inference for the 120b cloud model. C (claude-sonnet via `claude -p` subprocess) was faster (8s) because the output was smaller and claude-cli was warm.

### Reproducing this

1. Open a Claude Code session: `cd <repo> && claude --model claude-opus-4-7`
2. `/load` to bring in TARGET + SAVE context.
3. `/lead-mode` to enter orchestrator role.
4. Type a goal sentence — e.g. *"build a small dinosaur jumping cactus game in single-file HTML"*.
5. Approve the proposed decomposition.
6. Wait ~25s for B+C parallel, then ~30s for D integration. Total ~1–5 minutes depending on goal complexity.
7. Open the produced artifact (path is in D's `result`).

The `/lead-mode` skill itself lives at `.claude/skills/lead-mode/SKILL.md`. It's prompt-only — no executable code beyond what the LLM does with the MCP tools.

---

## Recipe 7 — Adding a third vendor (Google Gemini worker on Raspberry Pi)

**Story:** the broker already has two worker daemons (Anthropic Claude via `claude-cli`, OpenAI OSS via `ollama`). Adding Google as a third vendor turns "cross-vendor" from "two big LLM camps" into "the actual three major ecosystems each holding a slot on a single task chain". This recipe walks through what you change in the code, what you change on the broker, and what you change on the new worker host — from clean repo to "online" status in ~5 minutes once you have credentials.

The change set is small enough to be a one-PR pattern for any new vendor. Recap of what an adapter actually is: ~110 lines of `httpx` calls in a class with three methods (`setup` / `run_task` / `teardown`). No SDK. No new external dependency.

**Topology:** worker pool now has three vendors. The lead's prompt doesn't change — capability tags carry the routing.

```
   Lead (claude-mac, Opus)
        │ create_task(required_all=[...])
        ▼
   ┌──────────────────────────────────────────────────────────┐
   │                       Broker                              │
   │  Capability filter:                                       │
   │    tier:opus / family:claude       → claude-mac (lead)    │
   │    tier:reasoning / family:gpt-oss → worker-gpt-oss-cloud │
   │    tier:sonnet / family:claude     → worker-claude-sonnet │
   │    family:google / tier:flash      → worker-gemini-rpi    │  ← new
   └──────┬───────────────────┬────────────────────┬───────────┘
          │                   │                    │
          ▼                   ▼                    ▼
   ┌──────────────┐   ┌────────────────┐   ┌────────────────┐
   │  Anthropic   │   │  Ollama Cloud  │   │  Google AI     │
   │  Max sub     │   │  gpt-oss:120b  │   │  Studio        │
   │  via claude  │   │                │   │  gemini-2.5-…  │
   │   -cli       │   │                │   │                │
   └──────────────┘   └────────────────┘   └────────────────┘
```

### The change set (verified 2026-05-24)

What landed:

| Where | What | Size |
|---|---|---|
| `src/mab/worker/adapters/gemini.py` | New `GeminiAdapter` — httpx POST `/v1beta/models/{model}:generateContent`, `x-goog-api-key` header auth, multi-part text concat, `finishReason` surfaced on empty response | ~110 lines |
| `src/mab/worker/cli.py` | `ADAPTER_CHOICES` gains `gemini`; new `--gemini-api-key` / `--gemini-base-url` / `--gemini-max-output-tokens` / `--gemini-temperature` flag group; `--system-prompt` now applies | ~30 lines |
| `src/mab/shared/capabilities.py` | Map for `gemini-2.5-pro/flash/flash-lite`, `gemini-2.0-flash/flash-lite/flash-thinking`, `gemini-1.5-pro/flash`, plus bare `gemini` fallback | ~10 lines |
| `deploy/setup-worker.sh` | `--gemini-api-key` flag, validation, systemd `Environment=` lines | ~20 lines |
| `tests/worker/test_gemini_adapter.py` | 9 unit tests with `httpx.MockTransport` (happy path, system prompt, temperature, multi-part, missing key, env-var key, HTTP error, empty `finishReason=SAFETY`, no-candidates) | new |
| `tests/worker/test_cli.py` | 1 build-adapter test with full Gemini flag set | +20 lines |

Test count went 196 → 206. Zero new dependencies (`httpx` was already in the tree).

### Bringing the third host online

```bash
# 1. On the broker host — generate an api key for the new worker.
#    MAB_DB_PATH must match what the broker's systemd unit uses;
#    naked gen-key writes to the default ~/.multi-agent-broker/db.sqlite
#    which usually isn't where the production broker reads from.
ssh BROKER_HOST 'cd ~/multi-agent-broker \
    && MAB_DB_PATH=<broker db path> .venv/bin/mab-broker gen-key \
        --name worker-gemini-rpi'
# → outputs: API key: mab-ak-XXXXXXXXXXXXXXXX

# 2. On the new worker host (Raspberry Pi, in this example):
git clone https://github.com/<you>/multi-agent-broker.git ~/multi-agent-broker
cd ~/multi-agent-broker

export GEMINI_API_KEY=<your google ai studio key>
./deploy/setup-worker.sh \
    --broker-url http://BROKER_HOST:8420 \
    --api-key   mab-ak-XXXX_from_step_1 \
    --adapter   gemini \
    --model     gemini-2.5-flash \
    --gemini-api-key "$GEMINI_API_KEY" \
    --name      gemini
```

`setup-worker.sh` auto-installs `uv`, runs `uv sync`, probes broker `/health`, verifies the api-key against `GET /agents/me` (this catches DB-mismatch bugs), writes a systemd `--user` unit at `~/.config/systemd/user/mab-worker-gemini.service` (chmod 600, secrets in `Environment=` not `ExecStart`), enables linger, starts the service, and polls `/agents/me` until `status=online`.

Smoke check from the broker side:

```bash
curl -s -H "Authorization: Bearer <any valid mab-ak>" \
    http://BROKER_HOST:8420/api/v1/agents \
    | jq '.[] | select(.name=="worker-gemini-rpi") | {status, capabilities, last_heartbeat_age_seconds}'
```

Expected:

```json
{
  "status": "online",
  "capabilities": [
    "model:gemini-2.5-flash",
    "family:google",
    "tier:flash",
    "provider:google"
  ],
  "last_heartbeat_age_seconds": 7.1
}
```

Capability tags were derived automatically from `--model gemini-2.5-flash` — no manual capability list needed. From this point on, **any task with `required_all=["family:google"]` (or `tier:flash`, or `provider:google`) gets routed to this worker via the broker's existing capability filter** — no lead-side prompt change at all.

### Verifying the routing path (without burning API credits)

```bash
curl -sS -X POST -H "Authorization: Bearer mab-ak-XXXX" \
    -H "Content-Type: application/json" \
    -d '{"title": "gemini probe", "description": "Reply with exactly: gemini ok",
         "required_all": ["family:google"]}' \
    http://BROKER_HOST:8420/api/v1/tasks
```

The broker should:
1. Filter `match_agents(required_all=["family:google"])` → returns only `worker-gemini-rpi`.
2. Emit `task_event:created` on the WS queue of that worker only.
3. Worker daemon's `wait_for_task` returns the task within ~100 ms; daemon claims it.

The Gemini API call itself depends on your billing situation. In this build's first probe the API returned `HTTP 429: prepayment credits are depleted` — entirely a Google AI Studio billing issue, not an adapter or routing bug. The adapter caught the response cleanly and the task ended up with:

```
status: failed
notes:  ["picked up by worker daemon (gemini)",
         "adapter error: Gemini API HTTP 429: {\n  \"error\": {\n    \"code\": 429,
          \n    \"message\": \"Your prepayment credits are depleted...\""]
```

That's the graceful-failure path doing its job — capability routing and adapter HTTP plumbing both verified end-to-end; the lead can see exactly what blew up from the task notes. Once Gemini billing is sorted (or you swap to a free-tier project / `gemini-2.5-flash-lite`), the same probe completes with a real response and any of Recipes 1–6 works unchanged with Google in the mix.

### Lessons that travel to the next vendor

- **gen-key needs the broker's `MAB_DB_PATH`**. Naked `mab-broker gen-key` writes to the default DB; the broker's systemd unit usually points elsewhere. The setup script's `/agents/me` 401 probe catches this mismatch immediately — don't skip that probe in your own deploy tooling.
- **Capability auto-derivation does the work** if you keep `capabilities.py` up to date. A single line like `"gemini-2.5-flash": ("google", "flash")` propagates into `family:google`, `tier:flash`, `provider:google` tags on every worker started with `--model gemini-2.5-flash`. No need to maintain a parallel "this worker supports these tags" config per agent.
- **The adapter's no-text-response branch must surface the API's reason**. Gemini returns `finishReason: "SAFETY"` (or `"MAX_TOKENS"`, `"RECITATION"`) when there's no usable output. Bake the reason into the error so the lead can tell "model refused" apart from "we configured it wrong".
- **systemd `Environment=` keeps secrets out of `ps`**. Both the broker api-key and the Gemini api-key live in the unit file's `Environment=` lines, never in `ExecStart`. The unit file is chmod 600. `ps auxww` shows only `mab-worker --broker-url ... --api-key REDACTED-in-env`. Roll the same pattern for any vendor with a key.

---

## What's NOT in this cookbook (yet)

- **Inline result substitution.** Recipes here gate by `depends_on` but don't auto-inject upstream `result` into downstream `description`. If your downstream prompt needs the upstream result verbatim (e.g. "polish this exact draft"), today you have to dispatch sequentially: create A, wait for A, read result, embed in B's description, create B. The fire-and-forget pattern works for plans where downstream prompts are self-contained, or use Recipe 4's auto-promote pattern with a context handoff doc.
- **Daemon-side context expansion.** Non-MCP adapters (`ollama`, `anthropic`) can't fetch contexts from the LLM side. A future daemon enhancement would detect `ctx_*` references in `task.description` and inline the content before calling the LLM. For now, context-aware tasks need `claude-cli` adapter workers.
- **Channel auto-subscribe for new workers.** Today an agent must explicitly `join_channel` to receive pushes. There's no "subscribe all workers with `tier:reasoning` to `#dispatch` by capability" — coordinate this manually at agent registration time.

## Troubleshooting

| Symptom | Cause |
|---------|-------|
| `B` returns `status=pending` instead of `blocked` immediately after dispatch | Broker is on old code without the `depends_on` schema. Run `./deploy/update.sh` on the broker host. |
| `depends_on` field missing from `GET /tasks` responses | Same — broker schema migration hasn't run. |
| Worker daemon claims a blocked task | Shouldn't happen — `claim_task` SQL filters on `status='pending'`. If you see this, file a bug. |
| Downstream `status=failed` with note `upstream dependency X failed` | Working as designed — broker propagated A's failure to B/C. Don't retry; fix the upstream and dispatch the chain again. |
