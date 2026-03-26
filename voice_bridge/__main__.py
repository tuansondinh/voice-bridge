"""bridge_main.py — Entry point for the remote voice bridge server.

Usage::

    agent-voice-bridge                    # default: 0.0.0.0:8787
    agent-voice-bridge --port 9000        # custom port
    agent-voice-bridge --host 127.0.0.1   # localhost only
"""

from __future__ import annotations

import argparse
import socket
import sys


def _log(msg: str) -> None:
    print(f"[bridge] {msg}", file=sys.stderr, flush=True)


def _get_local_ip() -> str:
    """Get the machine's local network IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def run_bridge() -> None:
    """Start the voice bridge server."""
    parser = argparse.ArgumentParser(
        description="Remote voice bridge for Claude Code"
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=8787, help="Port (default: 8787)"
    )
    parser.add_argument(
        "--model",
        default="sonnet",
        help="Claude model to use (default: sonnet). Options: sonnet, opus, haiku",
    )
    args = parser.parse_args()

    # Check claude_agent_sdk is importable — the SDK bundles its own CLI,
    # so no PATH lookup is needed.
    from voice_bridge.claude import ClaudeSession

    if not ClaudeSession.check_available():
        _log("ERROR: claude-agent-sdk is not installed.")
        _log("Install it with: pip install claude-agent-sdk")
        _log("Then set up auth:")
        _log("  Claude Max (OAuth): claude setup-token  (then export CLAUDE_CODE_OAUTH_TOKEN)")
        _log("  API billing:        export ANTHROPIC_API_KEY=sk-...")
        sys.exit(1)

    # Check auth credentials are present
    import os as _os
    if not _os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") and not _os.environ.get("ANTHROPIC_API_KEY"):
        _log("ERROR: No authentication credentials found.")
        _log("Set one of:")
        _log("  CLAUDE_CODE_OAUTH_TOKEN — Claude Max subscription (run: claude setup-token)")
        _log("  ANTHROPIC_API_KEY       — Anthropic API key (pay-per-use billing)")
        sys.exit(1)

    # Load models eagerly
    from voice_bridge.server import AUTH_TOKEN, TAILSCALE_HOSTNAME, app, load_models, set_bridge_model

    set_bridge_model(args.model)
    load_models()

    # Print access URL
    local_ip = _get_local_ip()
    _log("")
    _log("=" * 60)
    _log("  Voice Bridge is ready!")
    _log("")
    _log(f"  Local:   http://localhost:{args.port}/?token={AUTH_TOKEN}")
    _log(f"  Network: http://{local_ip}:{args.port}/?token={AUTH_TOKEN}")
    if TAILSCALE_HOSTNAME:
        _log(f"  Tailscale: https://{TAILSCALE_HOSTNAME}:{args.port}/?token={AUTH_TOKEN}")
    _log("")
    _log("  Open the Network URL on your phone to start.")
    _log("  (Both devices must be on the same WiFi network,")
    if TAILSCALE_HOSTNAME:
        _log("   or use the Tailscale URL above for remote access)")
    else:
        _log("   or use Tailscale for remote access — install Tailscale and enable HTTPS)")
    _log("")
    if args.host == "0.0.0.0":
        _log("  WARNING: Server is bound to 0.0.0.0 and is reachable from")
        _log("  any device on your network. The auth token provides basic")
        _log("  protection. Use --host 127.0.0.1 to restrict to localhost.")
    _log("=" * 60)
    _log("")

    # Start uvicorn
    import uvicorn

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="warning",
        # Disable access logs (noisy with WebSocket)
        access_log=False,
    )


if __name__ == "__main__":
    run_bridge()
