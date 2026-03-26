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
```

Requires Claude Code CLI on PATH (`claude`).

---

## Using with VibeTunnel (recommended)

[VibeTunnel](https://vibetunnel.sh) creates a public HTTPS tunnel to your local server. This is the easiest way to use the bridge from a phone.

### 1. Start the bridge

```bash
BRIDGE_ALLOWED_ORIGIN=* voice-bridge
```

The terminal prints the token you'll need:

```
  Local:   http://localhost:8787/?token=abc123...
  Network: http://192.168.1.42:8787/?token=abc123...
```

> `BRIDGE_ALLOWED_ORIGIN=*` disables the origin whitelist so the tunnel's
> HTTPS origin is accepted. The auth token still protects the endpoint.
> For tighter security, set it to your specific tunnel URL instead:
> `BRIDGE_ALLOWED_ORIGIN=https://abc123.vt.dev`

### 2. Open the tunnel

In a separate terminal:

```bash
vibetunnel http 8787
```

VibeTunnel prints a public HTTPS URL:

```
https://abc123.vt.dev
```

### 3. Open on your phone

Append the token from step 1:

```
https://abc123.vt.dev/?token=abc123...
```

The page loads over HTTPS, microphone permission is granted, and the WebSocket connects as `wss://` automatically.

### Notes

- The token changes on every restart — update the URL if you restart the bridge.
- For a stable URL across restarts, set `AUTH_TOKEN` in your environment:
  ```bash
  AUTH_TOKEN=mytoken BRIDGE_ALLOWED_ORIGIN=* voice-bridge
  ```
  *(not yet implemented — use a process manager or keep the terminal open)*

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
