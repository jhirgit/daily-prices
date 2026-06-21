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

# Remote connector (what Claude.ai web/mobile speak): streamable HTTP
python mcp_server.py --host 0.0.0.0 --port 8000      # serves at /mcp

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
**Settings → Connectors → Add custom connector** → paste `https://<your-host>/mcp`.

**B. Quick test via a tunnel.** Run locally and expose it temporarily:

```bash
python mcp_server.py --port 8000
ngrok http 8000          # or: cloudflared tunnel --url http://localhost:8000
```

Add the tunnel's `https://…/mcp` URL as the connector.

> **Note on the data.** The DB is regenerated and committed daily by the GitHub
> Actions job. A hosted server reads whatever `prices.db` it was deployed with,
> so redeploy (or mount a synced copy / pull the latest `prices.db`) to pick up
> new days. The bundled Dockerfile bakes in the current DB and lets you mount a
> fresher one read-only: `-v "$PWD/prices.db:/app/prices.db:ro"`.

### Authentication

This server ships with **no auth** — fine for a quick personal/tunnelled test,
but anyone with the URL can read your watchlist and price history. For anything
long-lived, put it behind your host's access controls or an auth proxy before
sharing the URL.

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
