# Container for the daily-prices MCP server.
# Build:  docker build -t daily-prices-mcp .
# Run:    docker run -p 8000:8000 \
#           -e MCP_AUTH_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')" \
#           -v "$PWD/prices.db:/app/prices.db:ro" daily-prices-mcp
#
# With MCP_AUTH_TOKEN set, the endpoint is served at the secret path
# /<token>/mcp (the startup log prints a masked confirmation). Expose port 8000
# publicly over HTTPS and add https://<host>/<token>/mcp in Claude.ai under
# Settings -> Connectors. Without the token it serves an unauthenticated /mcp.
FROM python:3.12-slim

WORKDIR /app

COPY requirements-mcp.txt .
RUN pip install --no-cache-dir -r requirements-mcp.txt

COPY mcp_server.py .
# Bake in a copy of the DB; mount a fresh one read-only at runtime to override.
COPY prices.db .

ENV PRICES_DB=/app/prices.db
EXPOSE 8000

CMD ["python", "mcp_server.py", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
