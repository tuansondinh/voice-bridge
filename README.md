# voice-bridge

A FastAPI WebSocket server that connects a phone browser to Claude Code on your PC with voice and text chat.

- **Voice input** — Whisper STT (pywhispercpp, runs locally)
- **Voice output** — Kokoro TTS (runs locally, 24 kHz)
- **VAD** — Silero (energy gate + probability threshold, no false triggers)
- **Text input** — type instead of speak, same Claude session
- **Half-duplex** — mic auto-mutes during playback; tap Stop to interrupt

> **HTTPS is required.** Browsers only grant microphone access on secure origins
> (`https://` or `localhost`). A plain `http://` LAN URL will not work — even
> on the same WiFi. Use Cloudflare Tunnel or Tailscale HTTPS (see below).

---

## Quick Start

### 1. Install

```bash
pip install -e .
# or
uv sync
```

The `claude-agent-sdk` package is installed automatically as a dependency.

### 2. Set up authentication

Pick **one** option below:

#### Option A: Claude Max (OAuth, recommended)

```bash
# Generate a long-lived token
claude setup-token

# Copy the token and export it
export CLAUDE_CODE_OAUTH_TOKEN=<paste_token_here>
```

#### Option B: Anthropic API key (pay-per-use)

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

If neither is set, voice-bridge will exit with a clear error message.

### 3. Start the server

```bash
voice-bridge
```

The terminal prints your local URL with an auth token. Open it on your phone over HTTPS (see [Using with Cloudflare Tunnel](#using-with-cloudflare-tunnel-recommended) or [Using with Tailscale HTTPS](#using-with-tailscale-https)).

---

## Install

```bash
pip install -e .
# or
uv sync
```

The `claude-agent-sdk` package is installed automatically as a dependency.
It bundles the Claude Code CLI internally — no separate `claude` binary on PATH is needed.

### Authentication

Set **one** of these environment variables before starting voice-bridge:

**Option A — Claude Max subscription (OAuth, recommended):**

```bash
claude setup-token          # generates a long-lived OAuth token
export CLAUDE_CODE_OAUTH_TOKEN=<token from above>
```

**Option B — Anthropic API key (pay-per-use billing):**

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

voice-bridge exits immediately with a clear error message if neither variable is set.

## Start / restart

```bash
pkill -f "agent-voice-bridge" 2>/dev/null; pkill -f "uvicorn.*bridge" 2>/dev/null; sleep 1 && \
  nohup uv run voice-bridge --port 8787 > /tmp/bridge.log 2>&1 &
```

This kills any running bridge process and starts it fresh. Logs are at `/tmp/bridge.log`.

---

## Using with Tailscale HTTPS (recommended)

Tailscale gives every device a stable `*.ts.net` HTTPS URL — no public exposure, no tunnel relay, and no extra env vars needed.

### One-time setup

1. Install Tailscale: `brew install --cask tailscale`, sign in, connect.
2. In the [Tailscale admin console](https://login.tailscale.com/admin/dns), enable **HTTPS Certificates** under the DNS tab.
3. Enable the HTTPS proxy (run once, survives reboots):
   ```bash
   tailscale serve --bg http://localhost:8787
   ```

### Each session

```bash
pkill -f "voice.bridge" 2>/dev/null; sleep 1 && \
  CLAUDE_CODE_OAUTH_TOKEN=<your_token> \
  nohup uv run voice-bridge --port 8787 > /tmp/bridge.log 2>&1 &
```

Then check the log for your URL:

```bash
grep "Tailscale:" /tmp/bridge.log
```

Open the URL on your phone (no port number — `tailscale serve` handles port 443):

```
https://my-pc.tail1234.ts.net/?token=e085fe85...
```

The `.ts.net` origin is automatically added to the WebSocket allowlist. Traffic stays entirely within your Tailscale network.

---

## Sessions & Chat History

voice-bridge automatically saves all conversations as sessions in the Claude Agent SDK's local storage. The mobile UI includes a slide-out **Sessions drawer** (tap the `☰` hamburger icon) where you can:

- **View all past sessions** — titles, creation timestamps, relative times ("2 hours ago")
- **Switch sessions** — tap any session to resume its conversation context
- **Create new session** — tap the "+ New" button to start a fresh conversation
- **Rename sessions** — tap the `✎` edit icon to give a session a custom title
- **Delete sessions** — tap the `✕` delete icon to remove a session
- **Auto-resume** — the app remembers your last session and reopens it when you reload the page

### Under the Hood

- **Storage** — Sessions are stored as JSONL files in `~/.claude/projects/<cwd>/` (SDK default)
- **Auto-tagging** — Each session is tagged with `"voice-bridge"` to prevent mixing with unrelated Claude CLI sessions
- **REST API** — The server exposes `/api/sessions` endpoints for listing and managing sessions
- **Resumption** — When you switch or resume a session, the SDK replays the context so Claude remembers the conversation

The session drawer persists across browser reloads and WebSocket reconnects — your chat history is always accessible.

---

## Alternative: Cloudflare Tunnel

[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/) creates a free, no-login HTTPS tunnel to your local server. No account required for quick tunnels.

### 1. Start the bridge

```bash
BRIDGE_ALLOWED_ORIGIN=* voice-bridge
```

The terminal prints the token you'll need:

```
  Local:   http://localhost:8787/?token=e085fe85...
  Network: http://192.168.1.42:8787/?token=e085fe85...
```

> `BRIDGE_ALLOWED_ORIGIN=*` disables the origin whitelist so the tunnel's
> HTTPS origin is accepted. The auth token in the URL still protects the endpoint.

### 2. Open a Cloudflare Tunnel

Install `cloudflared` if you haven't:

```bash
brew install cloudflared
```

Then in a separate terminal:

```bash
cloudflared tunnel --url http://localhost:8787
```

Cloudflare prints a randomly-named public HTTPS URL:

```
https://monitors-takes-long-riverside.trycloudflare.com
```

### 3. Open on your phone

Append the token from step 1:

```
https://monitors-takes-long-riverside.trycloudflare.com/?token=e085fe85340b208b4d57eb82e74a6a79f8d745d24a172674270975ca7d0194bc
```

The page loads over HTTPS, microphone permission is granted, and the WebSocket connects as `wss://` automatically.

### Notes

- Both the tunnel URL and the token change on every restart — regenerate the full URL each time.
- Quick tunnels (`trycloudflare.com`) are ephemeral and free. For a stable named tunnel, [create a Cloudflare account](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) and run a persistent tunnel.

---

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `0.0.0.0` | Bind address |
| `--port` | `8787` | Port |
| `--model` | `sonnet` | Claude model (`sonnet`, `opus`, `haiku`) |

| Env var | Description |
|---------|-------------|
| `BRIDGE_ALLOWED_ORIGIN` | Extra allowed WebSocket origin. Set to `*` for any tunnel. |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude Max OAuth token (run `claude setup-token`). |
| `ANTHROPIC_API_KEY` | Anthropic API key for pay-per-use billing. |

---

## Troubleshooting

**"No authentication method available" error**
- Make sure you ran `claude setup-token` and exported the token:
  ```bash
  export CLAUDE_CODE_OAUTH_TOKEN=<your_token_here>
  ```
- Or use an API key instead: `export ANTHROPIC_API_KEY=sk-ant-...`

**"HTTPS is required" / microphone not working on phone**
- Do not use plain `http://` — it won't work on mobile
- Use Cloudflare Tunnel (see below) for free HTTPS
- Or use Tailscale HTTPS if you have Tailscale set up

**Slow first startup (10–15 seconds)**
- Models are loading for the first time (VAD, Whisper, TTS)
- Cached after that — subsequent startups are instant
- Check your internet (downloading ~400 MB of models)

---

## Requirements

- Python 3.12+
- `claude-agent-sdk` (installed automatically via `pip install -e .`)
- `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` environment variable
- macOS (Kokoro TTS uses MPS/CPU; Linux should work with CPU fallback)
