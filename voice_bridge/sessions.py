"""sessions.py — FastAPI router for session CRUD endpoints.

Provides REST API access to claude_agent_sdk session history so the
frontend drawer can list, rename, and delete conversations.

All endpoints require a ``token`` query param matching ``AUTH_TOKEN``.
Sessions are filtered to only those tagged "voice-bridge" to prevent
leaking unrelated Claude Code CLI sessions.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

try:
    from claude_agent_sdk import (
        SDKSessionInfo,
        SessionMessage,
        get_session_messages,
        list_sessions,
        rename_session,
        tag_session,
    )
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False
    SDKSessionInfo = None  # type: ignore[assignment,misc]
    SessionMessage = None  # type: ignore[assignment,misc]
    get_session_messages = None  # type: ignore[assignment,misc]
    list_sessions = None  # type: ignore[assignment,misc]
    rename_session = None  # type: ignore[assignment,misc]
    tag_session = None  # type: ignore[assignment,misc]

# Deferred import — AUTH_TOKEN is defined in server.py which imports this module.
# We reference it lazily via a function to avoid circular imports.
def _get_auth_token() -> str:
    from voice_bridge.server import AUTH_TOKEN
    return AUTH_TOKEN


def _log(msg: str) -> None:
    print(f"[bridge-sessions] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

def _require_auth(token: str = Query("")) -> str:
    """FastAPI dependency that validates the token query param."""
    if token != _get_auth_token():
        raise HTTPException(status_code=401, detail="Invalid token")
    return token


# ---------------------------------------------------------------------------
# Message format helper
# ---------------------------------------------------------------------------

def _format_message(msg: Any) -> dict:
    """Normalize a SessionMessage into a frontend-compatible dict.

    The SDK's ``SessionMessage.message`` is a raw Anthropic API dict. We
    normalize it into ``{role, text, tool_calls}`` for the frontend to render
    through the same UI model as live messages.

    Parameters
    ----------
    msg:
        A ``SessionMessage`` from the SDK.

    Returns
    -------
    dict
        ``{role: str, text: str, tool_calls: list[dict]}``
    """
    raw = msg.message if hasattr(msg, "message") else {}
    content = raw.get("content", "") if isinstance(raw, dict) else ""

    if isinstance(content, str):
        return {"role": msg.type, "text": content, "tool_calls": []}

    text_parts: list[str] = []
    tool_calls: list[dict] = []

    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text_parts.append(block.get("text", ""))
        elif block_type == "tool_use":
            tool_calls.append({
                "name": block.get("name", ""),
                "input": block.get("input", {}),
            })
        elif block_type == "tool_result":
            result_content = block.get("content", "")
            # content can be a list of blocks or a plain string
            if isinstance(result_content, list):
                result_text = " ".join(
                    b.get("text", "") for b in result_content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                result_text = str(result_content) if result_content else ""
            tool_calls.append({
                "name": "(result)",
                "content": result_text,
                "is_error": bool(block.get("is_error", False)),
            })

    return {
        "role": msg.type,
        "text": "".join(text_parts),
        "tool_calls": tool_calls,
    }


def _get_session_jsonl_path(session_id: str) -> Path | None:
    """Locate the JSONL file for a session scoped to the current working directory.

    Only searches the project directory that corresponds to the current cwd,
    preventing accidental access to sessions from other Claude Code projects.

    Returns ``None`` if not found.
    """
    cwd_slug = os.getcwd().replace("/", "-").lstrip("-")
    project_dir = Path.home() / ".claude" / "projects" / cwd_slug
    if not project_dir.exists():
        return None
    jsonl = project_dir / f"{session_id}.jsonl"
    return jsonl if jsonl.exists() else None


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class RenameRequest(BaseModel):
    title: str


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/sessions")


@router.get("")
async def list_voice_sessions(
    _token: str = Depends(_require_auth),
) -> JSONResponse:
    """List all voice-bridge sessions, sorted by last_modified descending.

    Filters to sessions tagged ``"voice-bridge"`` to prevent leaking
    unrelated Claude Code CLI conversations.

    Returns up to 50 most-recent sessions as::

        [{"id": ..., "title": ..., "summary": ..., "created_at": ..., "last_modified": ...}]
    """
    if not _SDK_AVAILABLE:
        return JSONResponse([])

    try:
        all_sessions = list_sessions(directory=os.getcwd(), limit=None)
    except Exception as exc:
        _log(f"list_sessions error: {exc}")
        return JSONResponse([])

    voice_sessions = [s for s in all_sessions if s.tag == "voice-bridge"]

    result = []
    for s in voice_sessions[:50]:
        title = s.custom_title or s.summary or s.first_prompt or "Untitled"
        result.append({
            "id": s.session_id,
            "title": title,
            "summary": s.summary or "",
            "created_at": s.created_at,
            "last_modified": s.last_modified,
        })

    return JSONResponse(result)


@router.get("/{session_id}/messages")
async def get_messages(
    session_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _token: str = Depends(_require_auth),
) -> JSONResponse:
    """Fetch and normalize messages for a session.

    Returns::

        [{"role": "user"|"assistant", "text": ..., "tool_calls": [...]}]
    """
    if not _SDK_AVAILABLE:
        raise HTTPException(status_code=503, detail="SDK not available")

    try:
        raw_messages = get_session_messages(
            session_id,
            directory=os.getcwd(),
            limit=limit,
            offset=offset,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        _log(f"get_session_messages error for {session_id}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to load messages")

    # Only include user/assistant messages (not system metadata)
    formatted = []
    for msg in raw_messages:
        if msg.type not in ("user", "assistant"):
            continue
        formatted.append(_format_message(msg))

    return JSONResponse(formatted)


@router.put("/{session_id}")
async def rename_voice_session(
    session_id: str,
    body: RenameRequest,
    _token: str = Depends(_require_auth),
) -> JSONResponse:
    """Rename a session (sets ``custom_title`` in the JSONL metadata)."""
    if not _SDK_AVAILABLE:
        raise HTTPException(status_code=503, detail="SDK not available")

    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title cannot be empty")

    try:
        rename_session(session_id, title, directory=os.getcwd())
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        _log(f"rename_session error for {session_id}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to rename session")

    return JSONResponse({"ok": True})


@router.delete("/{session_id}")
async def delete_voice_session(
    session_id: str,
    _token: str = Depends(_require_auth),
) -> JSONResponse:
    """Delete a session JSONL file permanently."""
    jsonl_path = _get_session_jsonl_path(session_id)
    if jsonl_path is None or not jsonl_path.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        jsonl_path.unlink()
        _log(f"Deleted session {session_id}")
    except OSError as exc:
        _log(f"delete session error for {session_id}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to delete session")

    return JSONResponse({"ok": True})
