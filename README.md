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
pkill -f "agent-voice-bridge" 2>/dev/null; pkill -f "uvicorn.*bridge" 2>/dev/null; pkill -f "cloudflared" 2>/dev/null; sleep 1 && \
  BRIDGE_ALLOWED_ORIGIN="*" nohup uv run voice-bridge --port 8787 > /tmp/bridge.log 2>&1 & \
  nohup cloudflared tunnel --url http://localhost:8787 > /tmp/cloudflared.log 2>&1 & \
  sleep 12 && \
  CF_URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' /tmp/cloudflared.log | head -1) && \
  TOKEN=$(grep -o 'token=[a-f0-9]*' /tmp/bridge.log | head -1) && \
  echo "$CF_URL/?$TOKEN"
```

This kills any running bridge and cloudflared processes, starts both fresh, and prints the full Cloudflare HTTPS URL with token after 12 seconds. Logs are at `/tmp/bridge.log` and `/tmp/cloudflared.log`.

---

## Using with Cloudflare Tunnel (recommended)

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

## Using with Tailscale HTTPS

Tailscale's [HTTPS feature](https://tailscale.com/kb/1153/enabling-https) gives every device a stable `*.ts.net` HTTPS URL — no public exposure, no tunnel relay.

```bash
# Enable HTTPS in Tailscale admin, then:
BRIDGE_ALLOWED_ORIGIN=* voice-bridge --host 0.0.0.0
```

Open on phone (replace with your machine's Tailscale HTTPS URL):

```
https://my-pc.tail1234.ts.net:8787/?token=<token>
```

Microphone works because the origin is HTTPS. Traffic stays within your Tailscale network.

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
