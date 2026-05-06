# bub-http-bridge

HTTP bridge channel plugin for bub. Provides a local HTTP endpoint that allows external processes to post messages into existing bub sessions.

## Use Case

When external harness agents (e.g. bub-codex running Codex CLI) need to interact with bub framework tools, they can POST messages to this bridge instead of requiring direct framework access.

## Configuration

```env
BUB_HTTP_BRIDGE_HOST=127.0.0.1  # bind address (default: localhost only)
BUB_HTTP_BRIDGE_PORT=9800       # listen port (default: 9800)
```

Enable via:
```env
BUB_ENABLED_CHANNELS=telegram,schedule,http-bridge
```

## API

### POST /message

Post a message into a bub session.

**Request:**
```json
{
  "session_id": "telegram:5779521985",
  "content": ",echo hello"
}
```

- `session_id`: target session (e.g. `telegram:5779521985`, `feishu:xxx`)
- `content`: message content. Use `,` prefix for command execution, plain text for agent processing.

**Response:** `202 Accepted`
```json
{"status": "accepted"}
```

## Usage Examples

```bash
# Execute a command in a session
curl -s -X POST http://localhost:9800/message \
  -H 'Content-Type: application/json' \
  -d '{"session_id": "telegram:5779521985", "content": ",echo done"}'

# Send a message for agent processing
curl -s -X POST http://localhost:9800/message \
  -H 'Content-Type: application/json' \
  -d '{"session_id": "telegram:5779521985", "content": "help me check the logs"}'
```

## Installation

```bash
uv pip install bub-http-bridge
```
