# voice-bridge

A FastAPI WebSocket server that connects a phone browser to Claude Code on your PC with voice and text chat.

- **Voice input** — Whisper STT (pywhispercpp, runs locally)
- **Voice output** — Kokoro TTS (runs locally, 24 kHz)
- **VAD** — Silero (energy gate + probability threshold, no false triggers)
- **Text input** — type instead of speak, same Claude session
- **Half-duplex** — mic auto-mutes during playback; tap Stop to interrupt

---

## Quick start (same WiFi)

```bash
# Install
pip install -e .

# Run
voice-bridge
```

The terminal prints two URLs:

```
Local:   http://localhost:8787/?token=<token>
Network: http://192.168.1.42:8787/?token=<token>
```

Open the **Network** URL on your phone. Both devices must be on the same WiFi.

---

## Using with VibeTunnel (remote / different network)

[VibeTunnel](https://vibetunnel.sh) creates a public HTTPS tunnel to your local server — useful when your phone and PC are on different networks, or when you want to share the bridge externally.

### 1. Start the bridge

```bash
BRIDGE_ALLOWED_ORIGIN=* voice-bridge
```

> `BRIDGE_ALLOWED_ORIGIN=*` disables the origin check. The auth token in the URL still protects the endpoint. Use a specific origin (e.g. `BRIDGE_ALLOWED_ORIGIN=https://abc123.vt.dev`) if you want tighter security.

### 2. Create a VibeTunnel

In a separate terminal:

```bash
vibetunnel http 8787
```

VibeTunnel will print a public URL like:

```
https://abc123.vt.dev
```

### 3. Build the phone URL

Grab the `token` value from the bridge terminal output, then append it:

```
https://abc123.vt.dev/?token=<token>
```

Open that URL on your phone. The bridge UI loads over HTTPS and the WebSocket upgrades to `wss://` automatically.

### Notes

- The token changes every time the bridge restarts — regenerate the URL if you restart.
- VibeTunnel's free tier may have connection limits; for continuous use consider Tailscale instead.
- For a persistent tunnel URL, run `vibetunnel http 8787 --name mybridge` (if your plan supports named tunnels).

---

## Using with Tailscale (persistent, no token fiddling)

Tailscale gives every device a stable private IP across networks.

```bash
# Both devices enrolled in the same Tailscale network
# PC Tailscale IP example: 100.64.0.5

voice-bridge --host 0.0.0.0
```

Open on phone:

```
http://100.64.0.5:8787/?token=<token>
```

No `BRIDGE_ALLOWED_ORIGIN` change needed — Tailscale traffic arrives as a LAN IP.

---

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `0.0.0.0` | Bind address |
| `--port` | `8787` | Port |

| Env var | Description |
|---------|-------------|
| `BRIDGE_ALLOWED_ORIGIN` | Extra allowed WebSocket origin. Set to `*` to allow all (tunnels). |

---

## Requirements

- Python 3.12+
- Claude Code CLI on PATH (`claude`)
- macOS (Kokoro TTS uses MPS/CPU; Linux should work with CPU fallback)
