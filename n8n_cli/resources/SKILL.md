---
name: n8n-cli
description: Manage n8n workflows, nodes, connections, pin-data, executions, credentials, and folders from Claude Code via the n8n-cli command. Use whenever the user mentions n8n workflows, debugging an n8n execution, patching a node, moving workflows between folders, or inspecting execution data. Outputs JSON on stdout so findings can be chained into follow-up commands.
---

# n8n-cli skill

Operate n8n through the `n8n-cli` command. The CLI is a thin, AI-first
wrapper over the n8n REST + frontend APIs. It parses workflows client-side
so you can act at node/connection/pin-data granularity and summarizes
execution data so multi-megabyte payloads fit in the context window.

## When to activate

- "Let me look at workflow X"
- "Why did execution 1234 fail?"
- "Patch the URL on the HTTP Request node in workflow Y"
- "Move these workflows into folder Z"
- "Pin some test data on the Start node"
- "Copy this workflow from staging to prod"
- Anything involving n8n instances, workflows, nodes, credentials, folders.

## Setup check

Before acting, confirm the CLI is ready:

```bash
n8n-cli --version                # ensure installed
n8n-cli instance current         # ensure an instance is configured + active
n8n-cli auth status              # ensure JWT + session are both valid
```

If any check fails, guide the user through `n8n-cli instance add` and
`n8n-cli auth login` rather than guessing.

## Canonical debug loop

Every fix follows this pattern. All outputs stay ≤1 KB by default; use
`--full` only when truly needed.

```bash
n8n-cli workflow structure <W>                         # 1. graph
n8n-cli execution list --workflow <W> --limit 5        # 2. find failure
n8n-cli execution-data get <EID> --node <N>            # 3. summarized output
n8n-cli node get --workflow <W> --name <N>             # 4. read config
n8n-cli credential list --for-node "<Node display name>"  # 5. auth context
n8n-cli node patch --workflow <W> --name <N> --set parameters.url=...  # 6. fix
n8n-cli pin-data set --workflow <W> --node Upstream --data '[...]'      # 7. seed
n8n-cli workflow execute <W> --wait --timeout 60       # 8. re-run
n8n-cli execution-data get <NEW_EID> --node <N>        # 9. verify
```

## Output contract

- Default: JSON on stdout (pipe into `jq` freely).
- Exit codes: 0 ok · 1 unimplemented · 2 user-error · 3 api-error ·
  4 auth-error · 5 capability-gated (enterprise license required).
- Credentials never return secret values.
- Binary payloads surface as `{mime_type, file_name, size_bytes}` — never
  base64 blobs.
- Use `--verbose` when the user needs to see which backend (public vs
  frontend) handled a call.

## Safety

- `workflow delete` requires `--force` and should almost always be replaced
  with `workflow archive`.
- Edits follow fetch → mutate → PUT: node renames cascade through
  connections + pinData automatically. Never hand-craft replacement JSON.
- Concurrency is last-write-wins. Warn the user if another editor might be
  active.

## Escape hatches

- `--full` — raw JSON when summarization hides what matters.
- `--file <path.json>` — full-replace workflow/node from disk.
- `--verbose` — log every HTTP call to stderr.
- `n8n-cli workflow export <id> --file x.json` — snapshot for git / manual
  inspection.

## Node typeVersion — handled automatically

`n8n-cli node add` omits `--type-version` by default. The CLI queries the
instance's `/types/nodes.json` catalog once per day and picks the latest
version for each node type (cached at
`~/.config/n8n-cli/cache/node-types-<instance>.yaml`). A built-in map of
common nodes ensures sane defaults even offline.

You only need `--type-version` when you specifically want a legacy version
(e.g. to match an existing workflow that was authored against `httpRequest`
v3). Parameter shapes are version-specific — always author params for the
latest version unless you set `--type-version` explicitly.

## Cross-workflow orchestration

Two patterns work; pick by intent:

- **Sub-workflow call** via `n8n-nodes-base.executeWorkflow` + child's
  `executeWorkflowTrigger`. Simplest, runs in the same execution context.
  Known to misbehave on some forked n8n builds — if it throws "Workflow
  does not exist", switch to the webhook pattern below.
- **Webhook fan-out**: child has `n8n-nodes-base.webhook` trigger with a
  unique `path`; parent has `n8n-nodes-base.httpRequest` node POSTing to
  `https://<instance>/webhook/<path>`. Child must be published (`workflow
  publish <id>`) so the production webhook is registered. Works on any
  n8n build and gives you parallel fan-out by adding multiple HTTP nodes
  from the same upstream.

## Common patterns

**Inspect a node's last run**
```bash
EID=$(n8n-cli execution list --workflow W --limit 1 | jq -r '.[0].id')
n8n-cli execution-data get "$EID" --node "My Node"
```

**Move every workflow in a folder to another project**
```bash
n8n-cli workflow list --folder-path "Ops/Billing" \
  | jq -r '.[].id' \
  | xargs -I{} n8n-cli workflow link --id {} --project PROJ_B
```

**Seed pin-data from a recorded run**
```bash
n8n-cli execution-data get 123 --node "HTTP Request" --full > /tmp/seed.json
n8n-cli pin-data set --workflow W --node "HTTP Request" --file /tmp/seed.json
```

For the full command surface run `n8n-cli --help` or
`n8n-cli <resource> --help`. The help text is the authoritative spec.
