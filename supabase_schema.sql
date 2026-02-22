-- Run this in your Supabase SQL Editor to create the required tables

-- Sessions: one per conversation, maps to Claude's session_id
CREATE TABLE IF NOT EXISTS sessions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id TEXT NOT NULL,
  claude_session_id TEXT,
  title TEXT DEFAULT 'New Chat',
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- Messages: chat history for display
CREATE TABLE IF NOT EXISTS messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id UUID REFERENCES sessions(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);

-- RLS: users can only access their own sessions and messages
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;

-- Drop existing policies if re-running (optional)
-- DROP POLICY IF EXISTS "Users can CRUD own sessions" ON sessions;
-- DROP POLICY IF EXISTS "Users can CRUD messages in own sessions" ON messages;

CREATE POLICY "Users can CRUD own sessions"
  ON sessions FOR ALL USING (auth.uid()::text = user_id);

CREATE POLICY "Users can CRUD messages in own sessions"
  ON messages FOR ALL
  USING (session_id IN (SELECT id FROM sessions WHERE user_id = auth.uid()::text));
