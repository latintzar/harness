# Native local workspace

This optional launcher opens the same bare workspace server, browser interface, access store and file store used by `harness/run.py`. It prepares one empty Organization conversation and six files copied from this repository: the vertical definition, reader role and four instruction documents. Open **Organization → Chat details → View all files** to inspect them.

## Start

From the cloned repository, after installing `requirements-dev.txt`:

```bash
.venv/bin/python tools/dev.py \
  --state "$HOME/.local/state/agent-workspace-local"
```

Open **http://127.0.0.1:8780** and enter the one-use code stored in the private `owner-code.txt` within that state directory. The normal owner interface supports local workspace edits. The launcher keeps network access on loopback and gives the process its own empty HOME and configuration directories. Use the standard [OpenAI setup](OPENAI-SETUP.md) and `harness/run.py` when commissioning managed execution with your own project, funded allowance, public MCP origin and connections.

## Public visitor mode

Use a separate, fresh state directory containing only material intended for public viewing:

```bash
.venv/bin/python tools/dev.py --guest \
  --state "$HOME/.local/state/agent-workspace-public" \
  --base-path /apps/agent-workspace/ \
  --public-origin https://preview.example.com
```

A reverse proxy forwards the complete prefix to the loopback server on port 8780. Choose another port with `--port`.

The visitor receives a real AccessStore session observing the starter's owner. Existing native handlers read the conversation, source files, capabilities and connection catalogue. The native observer UI opens existing Organization conversations and presents their files. Its composer and editing actions reflect the observer's permissions. The state contains an empty conversation; agent execution begins through the recipient's commissioned runtime.

`MountedApp` enforces GET/HEAD access for visitors, with session logout available. Observer changes, sign-in, file edits, agent creation, model turns and connection setup are held by the server. `agent_workspace_session` has an application-scoped Path, HttpOnly and SameSite=Strict; the exact HTTPS origin enables Secure. The prefix adapter changes deployment URLs on the server while requests continue to reach native handlers.

Keep this public state separate from a private workspace. The launcher requires an owned 0700 directory outside the checkout, marks its audience, and stores codes and runtime records privately. Provider credentials and other workspace paths are removed from its environment. A Python socket fence permits loopback transport; the supplied service unit also applies an operating-system network boundary.

## Run as a service

[The sample systemd unit](../deploy/agent-workspace.service) uses a dedicated Unix account, root-owned source at `/opt/atelier/agent-workspace/current`, a separate Python virtual environment and state at `/var/lib/atelier-agent-workspace`. Set the recipient's exact origin and chosen port before installation. Ingress, TLS and DNS belong to the recipient's host configuration.

The public Atelier deployment uses the same `tools/dev.py --guest` command and repository code. Its connection catalogue describes the included integrations; connected accounts and paid execution use the recipient's separately commissioned normal runtime.

## Verify

```bash
ROCOTO_VERTICAL=bare .venv/bin/python -m pytest -q \
  harness/tests/test_local_workspace.py harness/tests/test_web_mount.py
```

The local contracts start the real native backend with temporary private state, read its actual files and capability routes, verify observer permissions and reject mutation/observer escape requests. They also check the loopback boundary's request count and deployment URL/cookie handling.

[made by atelier neyra](https://atelierneyra.com)
