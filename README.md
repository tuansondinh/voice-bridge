# voice-bridge

A FastAPI WebSocket server that connects a phone browser to Claude Code on your PC with voice and text chat.

- **Voice input** — Whisper STT (pywhispercpp, runs locally)
- **Voice output** — Kokoro TTS (runs locally, 24 kHz)
- **VAD** — Silero (energy gate + probability threshold, no false triggers)
- **Text input** — type instead of speak, same Claude session
- **Half-duplex** — mic auto-mutes during playback; tap Stop to interrupt

> **HTTPS is required.** Browsers only grant microphone access on secure origins
> (`https://` or `localhost`). A plain `http://` LAN URL will not work — even
> on the same WiFi. Use VibeTunnel or Tailscale HTTPS (see below).

---

## Install

```bash
pip install -e .
# or
uv sync
```

Requires Claude Code CLI on PATH (`claude`).

## Start / restart

```bash
pkill -f "agent-voice-bridge" 2>/dev/null; pkill -f "uvicorn.*bridge" 2>/dev/null; sleep 1 && \
  cd /path/to/voice-bridge && \
  BRIDGE_ALLOWED_ORIGIN="*" nohup uv run voice-bridge --port 8787 > /tmp/bridge.log 2>&1 & \
  sleep 8 && grep "token=" /tmp/bridge.log | head -1
```

This kills any running bridge processes, starts fresh, and prints the token URL after 8 seconds. Logs are at `/tmp/bridge.log`.

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

| Env var | Description |
|---------|-------------|
| `BRIDGE_ALLOWED_ORIGIN` | Extra allowed WebSocket origin. Set to `*` for any tunnel. |

---

## Requirements

- Python 3.12+
- Claude Code CLI on PATH (`claude`)
- macOS (Kokoro TTS uses MPS/CPU; Linux should work with CPU fallback)
