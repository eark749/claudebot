# Claude Chatbot with Web Search

A simple chatbot using the Claude Agent Python SDK with web search, per-user sessions, JWT auth, and streaming responses. Data is stored in Supabase.

## Setup

1. **Supabase**
   - Create a project at [supabase.com](https://supabase.com)
   - Run `supabase_schema.sql` in the SQL Editor
   - Copy your project URL and service role key

2. **Environment**
   ```bash
   cp .env.example .env
   # Edit .env with your SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, and OPENAI_API_KEY (for quiz generation)
   ```

3. **Backend**
   ```bash
   cd backend
   pip install -r requirements.txt
   python main.py
   ```
   Runs at http://localhost:8000

4. **Frontend**
   - Open `frontend/index.html` in a browser, or serve it with any static server
   - Or: `cd frontend && python -m http.server 3000` then visit http://localhost:3000

5. **Claude Code CLI**
   - The SDK requires [Claude Code CLI](https://code.claude.com) to be installed and authenticated

## Quiz Feature (Teachers & Students)

- **Teachers**: Create quizzes from PDF/text documents (AI-generated via OpenAI), review in Quiz History, edit if needed, and send to students by grade (standard 1–12) with a due date.
- **Students**: See assigned quizzes, take them (MCQ), and submit before the due date.

Run `supabase_quiz_migration.sql` in the Supabase SQL Editor after the main schema. Add `OPENAI_API_KEY` to `.env` for quiz generation.

## API

- `POST /api/auth/signup` - Create account
- `POST /api/auth/login` - Sign in
- `POST /api/auth/refresh` - Refresh access token
- `GET /api/auth/me` - Current user (requires JWT)
- `POST /api/sessions` - Create session
- `GET /api/sessions` - List sessions
- `GET /api/sessions/{id}/messages` - Get message history
- `POST /api/sessions/{id}/chat` - Send message, stream response (SSE)
- `POST /api/quizzes/generate` - Generate quiz from document (teachers)
- `GET /api/quizzes` - List teacher's quizzes
- `GET /api/quizzes/{id}` - Get quiz with questions
- `PUT /api/quizzes/{id}` - Update quiz (draft only)
- `POST /api/quizzes/{id}/send` - Send quiz to students
- `GET /api/quiz-assignments` - List student's assigned quizzes
- `POST /api/quiz-assignments/{id}/submit` - Submit quiz (students)
