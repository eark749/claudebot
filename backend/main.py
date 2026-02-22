"""FastAPI app - auth, sessions, streaming chat."""

import asyncio
import io
import json
import uuid

from pypdf import PdfReader

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from queue import Empty, Queue
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from auth import get_current_user
from claude_service import stream_chat
from config import validate_config
from quiz_service import generate_quiz as do_generate_quiz
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


class ProfileUpdateRequest(BaseModel):
    role: str  # "teacher" | "student"
    standard: int | None = None  # 1-12 for students, None for teachers


class ChatRequest(BaseModel):
    message: str


class QuizGenerateRequest(BaseModel):
    standard: int  # 1-12
    total_marks: int = 10
    num_questions: int = 5


class QuizQuestionUpdate(BaseModel):
    question_text: str
    options: list[str]
    correct_answer: int
    marks: int


class QuizUpdateRequest(BaseModel):
    title: str | None = None
    due_at: str | None = None
    questions: list[QuizQuestionUpdate] | None = None


class QuizSendRequest(BaseModel):
    due_at: str  # ISO datetime string


class QuizSubmitRequest(BaseModel):
    answers: dict[str, int]  # question_id -> selected option index (0-based)


def _get_profile(user_id: str) -> tuple[str | None, int | None]:
    """Return (role, standard) for user."""
    supabase = get_supabase()
    r = supabase.table("user_profiles").select("role, standard").eq("user_id", user_id).execute()
    if not r.data or len(r.data) == 0:
        return (None, None)
    row = r.data[0]
    return (row.get("role"), row.get("standard"))


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


@app.get("/api/auth/profile")
async def get_profile(user: Annotated[dict, Depends(get_current_user)]):
    """Return user profile (role, standard). None if not set."""
    supabase = get_supabase()
    result = (
        supabase.table("user_profiles")
        .select("role, standard")
        .eq("user_id", user["id"])
        .execute()
    )
    if not result.data or len(result.data) == 0:
        return {"role": None, "standard": None}
    row = result.data[0]
    return {"role": row.get("role"), "standard": row.get("standard")}


@app.put("/api/auth/profile")
async def update_profile(
    body: ProfileUpdateRequest,
    user: Annotated[dict, Depends(get_current_user)],
):
    """Set or update user profile (role, standard for students)."""
    if body.role not in ("teacher", "student"):
        raise HTTPException(status_code=400, detail="role must be 'teacher' or 'student'")
    standard = None
    if body.role == "student":
        if body.standard is None or not (1 <= body.standard <= 12):
            raise HTTPException(status_code=400, detail="students must specify standard 1-12")
        standard = body.standard

    supabase = get_supabase()
    row = {
        "user_id": user["id"],
        "role": body.role,
        "standard": standard,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    supabase.table("user_profiles").upsert(row, on_conflict="user_id").execute()
    return {"role": body.role, "standard": standard}


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


# --- Quiz routes (JWT required) ---


@app.post("/api/quizzes/generate")
async def quiz_generate(
    user: Annotated[dict, Depends(get_current_user)],
    file: UploadFile = File(...),
    standard: int = Form(...),
    total_marks: int = Form(10),
    num_questions: int = Form(5),
):
    """Generate a quiz from document. Teachers only."""
    role, _ = _get_profile(user["id"])
    if role != "teacher":
        raise HTTPException(status_code=403, detail="Only teachers can create quizzes")
    if not (1 <= standard <= 12):
        raise HTTPException(status_code=400, detail="standard must be 1-12")
    if num_questions < 1 or num_questions > 20:
        raise HTTPException(status_code=400, detail="num_questions must be 1-20")

    doc_text = extract_text_from_file(file)
    if not doc_text.strip():
        raise HTTPException(status_code=400, detail="Could not extract text from document")

    try:
        questions = do_generate_quiz(
            document_content=doc_text,
            standard=standard,
            total_marks=total_marks,
            num_questions=num_questions,
            document_name=file.filename or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Quiz generation failed: {e}") from e

    supabase = get_supabase()
    quiz_row = {
        "teacher_id": user["id"],
        "title": (file.filename or "Quiz").replace(".pdf", "").replace(".txt", ""),
        "document_name": file.filename,
        "standard": standard,
        "total_marks": total_marks,
        "status": "draft",
    }
    qr = supabase.table("quizzes").insert(quiz_row).execute()
    if not qr.data or len(qr.data) == 0:
        raise HTTPException(status_code=500, detail="Failed to create quiz")
    quiz_id = qr.data[0]["id"]

    for i, q in enumerate(questions):
        supabase.table("quiz_questions").insert({
            "quiz_id": quiz_id,
            "order_idx": i,
            "question_text": q["question_text"],
            "question_type": "mcq",
            "marks": q["marks"],
            "options": q["options"],
            "correct_answer": q["correct_answer"],
        }).execute()

    result = (
        supabase.table("quizzes")
        .select("id, title, standard, total_marks, status, created_at")
        .eq("id", quiz_id)
        .single()
        .execute()
    )
    return {"quiz": result.data}


@app.get("/api/quizzes")
async def list_quizzes(user: Annotated[dict, Depends(get_current_user)]):
    """List teacher's quizzes. Teachers only."""
    role, _ = _get_profile(user["id"])
    if role != "teacher":
        raise HTTPException(status_code=403, detail="Only teachers can list quizzes")
    supabase = get_supabase()
    r = (
        supabase.table("quizzes")
        .select("id, title, standard, total_marks, status, created_at, sent_at, due_at")
        .eq("teacher_id", user["id"])
        .order("created_at", desc=True)
        .execute()
    )
    return {"quizzes": r.data or []}


@app.get("/api/quizzes/{quiz_id}")
async def get_quiz(
    quiz_id: uuid.UUID,
    user: Annotated[dict, Depends(get_current_user)],
):
    """Get quiz with questions. Teacher (owner) or student (assigned)."""
    supabase = get_supabase()
    role, standard = _get_profile(user["id"])
    q = supabase.table("quizzes").select("*").eq("id", str(quiz_id)).execute()
    if not q.data or len(q.data) == 0:
        raise HTTPException(status_code=404, detail="Quiz not found")
    quiz = q.data[0]
    if quiz["teacher_id"] == user["id"]:
        pass
    elif role == "student":
        a = supabase.table("quiz_assignments").select("id").eq("quiz_id", str(quiz_id)).eq("student_id", user["id"]).execute()
        if not a.data or len(a.data) == 0:
            raise HTTPException(status_code=404, detail="Quiz not found")
    else:
        raise HTTPException(status_code=403, detail="Access denied")

    questions = (
        supabase.table("quiz_questions")
        .select("id, order_idx, question_text, options, correct_answer, marks")
        .eq("quiz_id", str(quiz_id))
        .order("order_idx")
        .execute()
    )
    # For students, don't send correct_answer
    is_student = role == "student"
    qlist = []
    for qq in (questions.data or []):
        d = {k: v for k, v in qq.items() if k != "correct_answer" or not is_student}
        qlist.append(d)
    return {"quiz": quiz, "questions": qlist}


@app.put("/api/quizzes/{quiz_id}")
async def update_quiz(
    quiz_id: uuid.UUID,
    body: QuizUpdateRequest,
    user: Annotated[dict, Depends(get_current_user)],
):
    """Update quiz. Teachers only, draft only."""
    role, _ = _get_profile(user["id"])
    if role != "teacher":
        raise HTTPException(status_code=403, detail="Only teachers can edit quizzes")
    supabase = get_supabase()
    q = supabase.table("quizzes").select("id, status").eq("id", str(quiz_id)).eq("teacher_id", user["id"]).execute()
    if not q.data or len(q.data) == 0:
        raise HTTPException(status_code=404, detail="Quiz not found")
    if q.data[0]["status"] != "draft":
        raise HTTPException(status_code=400, detail="Cannot edit sent quiz")

    updates = {}
    if body.title is not None:
        updates["title"] = body.title
    if body.due_at is not None:
        updates["due_at"] = body.due_at
    if updates:
        supabase.table("quizzes").update(updates).eq("id", str(quiz_id)).execute()

    if body.questions is not None:
        supabase.table("quiz_questions").delete().eq("quiz_id", str(quiz_id)).execute()
        for i, qq in enumerate(body.questions):
            supabase.table("quiz_questions").insert({
                "quiz_id": str(quiz_id),
                "order_idx": i,
                "question_text": qq.question_text,
                "question_type": "mcq",
                "marks": qq.marks,
                "options": qq.options,
                "correct_answer": qq.correct_answer,
            }).execute()

    result = supabase.table("quizzes").select("*").eq("id", str(quiz_id)).single().execute()
    return {"quiz": result.data}


@app.post("/api/quizzes/{quiz_id}/send")
async def send_quiz(
    quiz_id: uuid.UUID,
    body: QuizSendRequest,
    user: Annotated[dict, Depends(get_current_user)],
):
    """Send quiz to students of that standard. Teachers only."""
    role, _ = _get_profile(user["id"])
    if role != "teacher":
        raise HTTPException(status_code=403, detail="Only teachers can send quizzes")
    supabase = get_supabase()
    q = supabase.table("quizzes").select("id, standard, status").eq("id", str(quiz_id)).eq("teacher_id", user["id"]).execute()
    if not q.data or len(q.data) == 0:
        raise HTTPException(status_code=404, detail="Quiz not found")
    if q.data[0]["status"] == "sent":
        raise HTTPException(status_code=400, detail="Quiz already sent")
    standard = q.data[0]["standard"]

    students = (
        supabase.table("user_profiles")
        .select("user_id")
        .eq("role", "student")
        .eq("standard", standard)
        .execute()
    )
    student_ids = [s["user_id"] for s in (students.data or [])]
    now = datetime.now(timezone.utc).isoformat()
    for sid in student_ids:
        try:
            supabase.table("quiz_assignments").insert({
                "quiz_id": str(quiz_id),
                "student_id": sid,
                "status": "pending",
            }).execute()
        except Exception:
            pass

    supabase.table("quizzes").update({
        "status": "sent",
        "sent_at": now,
        "due_at": body.due_at,
        "updated_at": now,
    }).eq("id", str(quiz_id)).execute()

    return {"sent_to": len(student_ids), "due_at": body.due_at}


@app.get("/api/quiz-assignments")
async def list_quiz_assignments(user: Annotated[dict, Depends(get_current_user)]):
    """List student's assigned quizzes. Students only."""
    role, _ = _get_profile(user["id"])
    if role != "student":
        raise HTTPException(status_code=403, detail="Only students can view assignments")
    supabase = get_supabase()
    r = (
        supabase.table("quiz_assignments")
        .select("id, quiz_id, status, submitted_at, created_at")
        .eq("student_id", user["id"])
        .order("created_at", desc=True)
        .execute()
    )
    assignments = r.data or []
    out = []
    for a in assignments:
        q = supabase.table("quizzes").select("id, title, total_marks, due_at").eq("id", a["quiz_id"]).single().execute()
        if q.data:
            out.append({"assignment": a, "quiz": q.data})
    return {"assignments": out}


@app.post("/api/quiz-assignments/{assignment_id}/submit")
async def submit_quiz(
    assignment_id: uuid.UUID,
    body: QuizSubmitRequest,
    user: Annotated[dict, Depends(get_current_user)],
):
    """Student submits quiz answers."""
    role, _ = _get_profile(user["id"])
    if role != "student":
        raise HTTPException(status_code=403, detail="Only students can submit quizzes")
    supabase = get_supabase()
    a = (
        supabase.table("quiz_assignments")
        .select("id, quiz_id, status")
        .eq("id", str(assignment_id))
        .eq("student_id", user["id"])
        .execute()
    )
    if not a.data or len(a.data) == 0:
        raise HTTPException(status_code=404, detail="Assignment not found")
    if a.data[0]["status"] == "submitted":
        raise HTTPException(status_code=400, detail="Already submitted")

    questions = (
        supabase.table("quiz_questions")
        .select("id, correct_answer, marks")
        .eq("quiz_id", a.data[0]["quiz_id"])
        .execute()
    )
    qmap = {str(q["id"]): q for q in (questions.data or [])}
    score = 0
    for qid, chosen in body.answers.items():
        if qid in qmap and qmap[qid]["correct_answer"] == chosen:
            score += qmap[qid]["marks"]

    now = datetime.now(timezone.utc).isoformat()
    supabase.table("quiz_assignments").update({
        "status": "submitted",
        "answers": body.answers,
        "score": score,
        "submitted_at": now,
    }).eq("id", str(assignment_id)).execute()

    return {"score": score, "submitted_at": now}


def sse_format(event: str, data: str) -> str:
    """Format one SSE message."""
    return f"event: {event}\ndata: {data}\n\n"


def extract_text_from_file(file: UploadFile) -> str:
    """Extract text from PDF or plain text file. Raises HTTPException on failure."""
    filename = (file.filename or "").lower()
    content = file.file.read()
    try:
        if filename.endswith(".pdf"):
            reader = PdfReader(io.BytesIO(content))
            parts = []
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    parts.append(text)
            return "\n\n".join(parts) if parts else ""
        if filename.endswith(".txt") or filename.endswith(".text"):
            return content.decode("utf-8", errors="replace")
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Use PDF (.pdf) or text (.txt) files.",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read file: {e}") from e


def get_system_prompt_for_user(role: str | None, standard: int | None) -> str | None:
    """Build role- and grade-appropriate system prompt for Claude."""
    if role == "teacher":
        return (
            "You are assisting an educator. The user is a teacher. Adapt your responses accordingly: "
            "Use clear, professional language. Suggest lesson ideas, explanations, and activities when relevant. "
            "Offer scaffolding and differentiation tips. Help with assessment and curriculum alignment. "
            "Be supportive and resourceful."
        )
    if role == "student" and standard is not None:
        # Group by approximate age: 1-3 (~6-8), 4-6 (~9-11), 7-9 (~12-14), 10-12 (~15-17)
        if standard <= 3:
            return (
                "The user is a student in Standard 1-3 (approximately 6-8 years old). "
                "Use very simple vocabulary and short sentences. Be warm, encouraging, and patient. "
                "Explain things step by step. Avoid jargon; if you use new words, define them simply. "
                "Use examples from everyday life they can relate to."
            )
        if standard <= 6:
            return (
                "The user is a student in Standard 4-6 (approximately 9-11 years old). "
                "Use clear, accessible language. Prefer shorter sentences and concrete examples. "
                "Be encouraging and supportive. Explain concepts without assuming much prior knowledge. "
                "Define technical terms when you use them."
            )
        if standard <= 9:
            return (
                "The user is a student in Standard 7-9 (approximately 12-14 years old). "
                "Use age-appropriate vocabulary. You can go into more depth while keeping explanations clear. "
                "Be helpful and engaging. Connect concepts to things they may have learned in school. "
                "Avoid overly complex or abstract language unless the topic requires it."
            )
        # 10-12
        return (
            "The user is a student in Standard 10-12 (approximately 15-17 years old). "
            "Use clear, more advanced vocabulary suitable for high school. "
            "You can discuss topics in greater depth and use more sophisticated explanations. "
            "Be supportive while challenging them appropriately. Assume growing academic maturity."
        )
    return None


@app.post("/api/sessions/{session_id}/chat")
async def chat(
    session_id: uuid.UUID,
    user: Annotated[dict, Depends(get_current_user)],
    message: str = Form(...),
    file: UploadFile | None = File(None),
):
    """Send a message and stream the response via SSE. Optional file (PDF or .txt) for document Q&A."""
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

    # Build prompt: include document content if file was uploaded
    prompt_text = message
    if file and file.filename:
        doc_text = extract_text_from_file(file)
        if doc_text.strip():
            prompt_text = (
                f"The user has attached a document (\"{file.filename}\"). "
                f"Here is its content:\n\n--- Document content ---\n{doc_text}\n--- End document ---\n\n"
                f"User question: {message}"
            )
        else:
            raise HTTPException(
                status_code=400,
                detail="Could not extract text from the file. The PDF may be empty or image-based.",
            )

    # Save user message (store original message for display)
    supabase.table("messages").insert(
        {
            "session_id": str(session_id),
            "role": "user",
            "content": message + (" (with attached document)" if file and file.filename else ""),
        }
    ).execute()

    # Fetch user profile for role-aware system prompt
    profile_result = (
        supabase.table("user_profiles")
        .select("role, standard")
        .eq("user_id", user["id"])
        .execute()
    )
    role = None
    standard = None
    if profile_result.data and len(profile_result.data) > 0:
        row = profile_result.data[0]
        role = row.get("role")
        standard = row.get("standard")
    system_prompt = get_system_prompt_for_user(role, standard)
    if file and file.filename:
        doc_instruction = (
            "The user has provided a document with their question. "
            "Answer based on the document content. Cite or refer to specific parts when relevant. "
            "If the answer is not in the document, say so clearly."
        )
        system_prompt = f"{doc_instruction}\n\n{system_prompt}" if system_prompt else doc_instruction

    # Use a thread + sync Queue so the SDK runs in an isolated event loop,
    # avoiding "exit cancel scope in different task" from anyio when the HTTP request ends.
    thread_queue: Queue[tuple[str, str | None] | None] = Queue()
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=1)

    def run_in_thread():
        async def _produce():
            try:
                async for event_type, data in stream_chat(
                    prompt=prompt_text,
                    claude_session_id=claude_session_id,
                    system_prompt=system_prompt,
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
