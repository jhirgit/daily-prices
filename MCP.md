# Querying your prices from Claude.ai

`mcp_server.py` is an [MCP](https://modelcontextprotocol.io) server that exposes
the `prices.db` collected by `fetch_prices.py` to an LLM client — so you can ask
Claude things like *"how did NVDA do this week?"* or *"which of my tickers gained
the most?"* and it queries the database for you.

Access is **read-only**: the server opens the database in read-only mode and the
raw-SQL tool rejects anything that isn't a single `SELECT`/`WITH` statement.

## Tools Claude gets

| Tool | What it does |
|------|--------------|
| `list_tickers` | Every symbol, its bar count, and date coverage |
| `get_price_history` | Daily OHLC bars for one ticker over a date range |
| `get_latest_quote` | Newest daily close + newest delayed spot quote |
| `compare_performance` | % return of several tickers over a window, ranked |
| `run_sql` | A single read-only `SELECT`/`WITH` query (escape hatch, max 1000 rows) |

## Run it

```bash
pip install -r requirements-mcp.txt

# Set a secret so the endpoint isn't public (see Authentication below)
export MCP_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"

# Remote connector (what Claude.ai web/mobile speak): streamable HTTP
python mcp_server.py --host 0.0.0.0 --port 8000      # serves at /<token>/mcp

# Local Claude Desktop instead
python mcp_server.py --transport stdio
```

Point it at a database elsewhere with `PRICES_DB=/path/to/prices.db`.

## Connect to Claude.ai chat (remote connector)

Claude.ai custom connectors talk to a **public HTTPS** MCP endpoint, so the
server has to be reachable from the internet — it can't see a database on your
laptop. Two common paths:

**A. Host it.** Run the container on any small host (Fly.io, Render, a VM, etc.):

```bash
docker build -t daily-prices-mcp .
docker run -p 8000:8000 daily-prices-mcp
```

Put it behind HTTPS (the platform's TLS, or a reverse proxy), then in Claude.ai:
**Settings → Connectors → Add custom connector** → paste
`https://<your-host>/<token>/mcp` (the startup log prints a masked confirmation
of the path).

**B. Quick test via a tunnel.** Run locally and expose it temporarily:

```bash
python mcp_server.py --port 8000
ngrok http 8000          # or: cloudflared tunnel --url http://localhost:8000
```

Add the tunnel's `https://…/<token>/mcp` URL as the connector.

> **Note on the data.** The DB is regenerated and committed daily by the GitHub
> Actions job. A hosted server reads whatever `prices.db` it was deployed with,
> so redeploy (or mount a synced copy / pull the latest `prices.db`) to pick up
> new days. The bundled Dockerfile bakes in the current DB and lets you mount a
> fresher one read-only: `-v "$PWD/prices.db:/app/prices.db:ro"`.

### Authentication

Claude.ai custom connectors authenticate with **OAuth only** — its UI has no
field for a static bearer token or API key. So this server uses the one thing
Claude.ai *can* carry: a **secret URL**.

Set `MCP_AUTH_TOKEN` to a long random value and the endpoint moves from the
public `/mcp` to an unguessable `/<token>/mcp`; the token *is* the credential
(like a private webhook URL). Requests to `/mcp` or a wrong token get a 404.

```bash
export MCP_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

Then the connector URL is `https://<your-host>/<token>/mcp`. Treat that whole
URL as a password: serve it only over **HTTPS** (so the path is encrypted in
transit) and don't paste it anywhere public. Rotate by changing the token.

Caveats of secret-URL auth: the token can appear in server/reverse-proxy access
logs and is single-tier (no per-user revocation). That's an acceptable bar for a
personal, read-only price history. If you later want real per-user auth with
consent and revocation, the heavier upgrade is a full **OAuth 2.1** flow — the
MCP Python SDK supports it via a token verifier, but it requires running (or
proxying to) an authorization server, which is overkill for one user.

Without `MCP_AUTH_TOKEN` the server logs a warning and serves an unauthenticated
`/mcp` — only do that on a trusted network or a throwaway tunnel.

## Claude Desktop (local, no hosting)

Add to `claude_desktop_config.json` (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "daily-prices": {
      "command": "python",
      "args": ["/absolute/path/to/daily-prices/mcp_server.py", "--transport", "stdio"]
    }
  }
}
```

Restart Claude Desktop; the daily-prices tools appear in the chat's tool menu.
