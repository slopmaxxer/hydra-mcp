# hydra-mcp

An [MCP](https://modelcontextprotocol.io) server for [Hydra](https://github.com/NixOS/hydra),
the Nix continuous integration server. It lets an agent look at projects, jobsets, evaluations,
builds, build steps, logs and the queue, and (with an account) trigger evaluations, restart,
cancel or bump builds and create or edit projects and jobsets. It works with NixOS Hydra and its
forks (Lix's Hydra), and can talk to several instances at once.

## Tools

| Tool | What |
|---|---|
| `list_instances` | Configured instances, the default, whether each can log in |
| `list_projects`, `get_project` | Projects; a project's jobsets with error flags and build counts |
| `get_jobset` | Configuration, evaluation state, eval/fetch errors, recent evaluations |
| `list_evals`, `get_eval` | Evaluations (inputs/flake rev, counts), builds filtered by status |
| `get_build` | Status, timing, derivation, outputs, products, metrics; failed, running and newest build steps (`steps="all"` for every one) |
| `get_build_log` | Tail of a build's log or of one build step (e.g. the failed dependency) |
| `get_job` | A job's build in the newest evaluation and its latest successful build |
| `get_queue` | Queue size, top of the queue, running builds |
| `search` | Projects, jobsets, jobs and builds by name or store path |
| `trigger_evaluation` | Schedule a jobset evaluation (`/api/push`) |
| `restart_build`, `cancel_build`, `bump_build` | Build actions |
| `manage_eval` | Restart failed/aborted builds of an evaluation, cancel or bump them |
| `put_project`, `put_jobset` | Create, or change only the given fields of, a project or jobset |

There is also a `debug_jobset` prompt. Read tools work anonymously on public instances; write tools
log in with the configured account and need the matching Hydra role (create-projects,
restart-jobs, cancel-build, bump-to-front, or project ownership).

Notes on Hydra's API that the tools handle for you: boolean fields of projects and jobsets are true
whenever the key is present (so `put_*` omits false flags rather than sending `0`), `latest` URLs
mean the latest *successful* build, build steps only exist on the HTML build page, and POST requests
need a `Referer`/`Origin` matching the instance.

## Configuration

`~/.config/hydra-mcp/config.toml` (or `$HYDRA_MCP_CONFIG`, or `--config`):

```toml
default = "nixos"

[instances.nixos]
url = "https://hydra.nixos.org"

[instances.mine]
url = "https://hydra.example.org"
username = "alice"                         # or username_file = "~/.secrets/hydra/username"
password_file = "~/.secrets/hydra/password"
```

Passwords are only read from files, at login time; they never appear in the config, the process
arguments or tool results. URLs must be HTTPS (plain HTTP only for loopback), and the client never
follows redirects to another host. The file is re-read whenever it changes (including a Home
Manager switch that repoints its symlink), so no restart is needed. Without a config file, `HYDRA_URL` (default
`https://hydra.nixos.org`), `HYDRA_USERNAME` and `HYDRA_PASSWORD_FILE` describe a single instance.

## Running

```sh
nix run github:slopinger/hydra-mcp            # stdio
uv run hydra-mcp --transport streamable-http   # http://127.0.0.1:8002/mcp, no authentication
```

Claude Code: `claude mcp add -s user hydra -- nix run github:slopinger/hydra-mcp`, or point
`command` at a built `result/bin/hydra-mcp`. Logs, descriptions and error messages come from builds
and other users: clients should treat them as data, not instructions.

## Development

```sh
nix develop         # editable environment with pytest
uv run pytest       # offline tests: config, client against a mock Hydra, MCP stdio handshake
uv run pytest -m live   # a few read-only calls to a real instance (HYDRA_LIVE_URL)
nix build && nix flake check
```

MIT licensed.
