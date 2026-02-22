"""FastAPI app - auth, sessions, streaming chat."""

import asyncio
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from queue import Empty, Queue
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from auth import get_current_user
from claude_service import stream_chat
from config import validate_config
from supabase_client import get_supabase


app = FastAPI(title="Claude Chatbot API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Request/Response models ---


class SignupRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class ChatRequest(BaseModel):
    message: str


# --- Auth routes (no JWT required) ---


@app.post("/api/auth/signup")
async def signup(body: SignupRequest):
    """Create new user account."""
    supabase = get_supabase()
    try:
        response = supabase.auth.sign_up(
            {"email": body.email, "password": body.password}
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    user = response.user
    session = response.session
    if not user:
        raise HTTPException(status_code=400, detail="Signup failed")
    return {
        "user": {"id": str(user.id), "email": user.email},
        "access_token": session.access_token if session else None,
        "refresh_token": session.refresh_token if session else None,
    }


@app.post("/api/auth/login")
async def login(body: LoginRequest):
    """Sign in and return tokens."""
    supabase = get_supabase()
    try:
        response = supabase.auth.sign_in_with_password(
            {"email": body.email, "password": body.password}
        )
    except Exception as e:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    session = response.session
    user = response.user
    if not session or not user:
        raise HTTPException(status_code=401, detail="Login failed")
    return {
        "user": {"id": str(user.id), "email": user.email},
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
    }


@app.post("/api/auth/refresh")
async def refresh(body: RefreshRequest):
    """Get new access token from refresh token. Returns new refresh_token too (old one is invalidated)."""
    supabase = get_supabase()
    try:
        response = supabase.auth.refresh_session(body.refresh_token)
    except Exception as e:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    session = response.session
    user = response.user
    if not session or not user:
        raise HTTPException(status_code=401, detail="Refresh failed")
    return {
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
        "user": {"id": str(user.id), "email": user.email},
    }


@app.get("/api/auth/me")
async def me(user: Annotated[dict, Depends(get_current_user)]):
    """Return current user (validates JWT)."""
    return user


# --- Session routes (JWT required) ---


@app.post("/api/sessions")
async def create_session(user: Annotated[dict, Depends(get_current_user)]):
    """Create a new chat session."""
    supabase = get_supabase()
    row = {
        "user_id": user["id"],
        "title": "New Chat",
        "claude_session_id": None,
    }
    result = supabase.table("sessions").insert(row).execute()
    if not result.data or len(result.data) == 0:
        raise HTTPException(status_code=500, detail="Failed to create session")
    s = result.data[0]
    return {"id": s["id"], "title": s["title"]}


@app.get("/api/sessions")
async def list_sessions(user: Annotated[dict, Depends(get_current_user)]):
    """List all sessions for the current user."""
    supabase = get_supabase()
    result = (
        supabase.table("sessions")
        .select("id, title, created_at, updated_at")
        .eq("user_id", user["id"])
        .order("updated_at", desc=True)
        .execute()
    )
    return {"sessions": result.data or []}


@app.delete("/api/sessions/{session_id}")
async def delete_session(
    session_id: uuid.UUID,
    user: Annotated[dict, Depends(get_current_user)],
):
    """Delete a session and all its messages."""
    supabase = get_supabase()
    sess = (
        supabase.table("sessions")
        .select("id")
        .eq("id", str(session_id))
        .eq("user_id", user["id"])
        .execute()
    )
    if not sess.data or len(sess.data) == 0:
        raise HTTPException(status_code=404, detail="Session not found")
    supabase.table("messages").delete().eq(
        "session_id", str(session_id)
    ).execute()
    supabase.table("sessions").delete().eq("id", str(session_id)).execute()
    return {"ok": True}


@app.get("/api/sessions/{session_id}/messages")
async def get_messages(
    session_id: uuid.UUID,
    user: Annotated[dict, Depends(get_current_user)],
):
    """Get message history for a session."""
    supabase = get_supabase()
    # Verify session belongs to user
    sess = (
        supabase.table("sessions")
        .select("id")
        .eq("id", str(session_id))
        .eq("user_id", user["id"])
        .execute()
    )
    if not sess.data or len(sess.data) == 0:
        raise HTTPException(status_code=404, detail="Session not found")
    result = (
        supabase.table("messages")
        .select("id, role, content, created_at")
        .eq("session_id", str(session_id))
        .order("created_at", desc=False)
        .execute()
    )
    return {"messages": result.data or []}


def sse_format(event: str, data: str) -> str:
    """Format one SSE message."""
    return f"event: {event}\ndata: {data}\n\n"


@app.post("/api/sessions/{session_id}/chat")
async def chat(
    session_id: uuid.UUID,
    body: ChatRequest,
    user: Annotated[dict, Depends(get_current_user)],
):
    """Send a message and stream the response via SSE."""
    supabase = get_supabase()
    # Load session, verify ownership
    sess_result = (
        supabase.table("sessions")
        .select("id, claude_session_id")
        .eq("id", str(session_id))
        .eq("user_id", user["id"])
        .execute()
    )
    if not sess_result.data or len(sess_result.data) == 0:
        raise HTTPException(status_code=404, detail="Session not found")
    session_row = sess_result.data[0]
    claude_session_id = session_row.get("claude_session_id")

    # Save user message
    supabase.table("messages").insert(
        {
            "session_id": str(session_id),
            "role": "user",
            "content": body.message,
        }
    ).execute()

    # Use a thread + sync Queue so the SDK runs in an isolated event loop,
    # avoiding "exit cancel scope in different task" from anyio when the HTTP request ends.
    thread_queue: Queue[tuple[str, str | None] | None] = Queue()
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=1)

    def run_in_thread():
        async def _produce():
            try:
                async for event_type, data in stream_chat(
                    prompt=body.message,
                    claude_session_id=claude_session_id,
                ):
                    thread_queue.put((event_type, data))
            finally:
                thread_queue.put(None)

        try:
            asyncio.run(_produce())
        except RuntimeError:
            pass

    async def generate():
        future = loop.run_in_executor(executor, run_in_thread)
        raw_response_chunks: list[str] = []
        final_session_id = None

        while True:
            try:
                item = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: thread_queue.get(timeout=600)
                )
            except Empty:
                break
            if item is None:
                break
            event_type, data = item
            if event_type == "thinking":
                if data:
                    yield sse_format(
                        "message",
                        json.dumps({"type": "thinking", "content": data}),
                    )
            elif event_type == "text":
                if data:
                    raw_response_chunks.append(data)
                    yield sse_format(
                        "message",
                        json.dumps({"type": "text", "content": data}),
                    )
            elif event_type == "done":
                final_session_id = data

        try:
            await future
        except RuntimeError:
            pass

        full_response = "".join(raw_response_chunks)

        yield sse_format(
            "done",
            json.dumps({"session_id": final_session_id}),
        )

        if full_response:
            supabase.table("messages").insert(
                {
                    "session_id": str(session_id),
                    "role": "assistant",
                    "content": full_response,
                }
            ).execute()
        if final_session_id:
            supabase.table("sessions").update(
                {
                    "claude_session_id": final_session_id,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("id", str(session_id)).execute()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.on_event("startup")
async def startup():
    validate_config()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
