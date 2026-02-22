-- Quiz tables migration - run this in Supabase SQL Editor after supabase_schema.sql

-- Quizzes: created by teachers
CREATE TABLE IF NOT EXISTS quizzes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  teacher_id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT 'Untitled Quiz',
  document_name TEXT,
  standard INTEGER NOT NULL CHECK (standard >= 1 AND standard <= 12),
  total_marks INTEGER NOT NULL DEFAULT 10,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'sent')),
  due_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  sent_at TIMESTAMPTZ
);

-- Quiz questions (MCQ)
CREATE TABLE IF NOT EXISTS quiz_questions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  quiz_id UUID NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
  order_idx INTEGER NOT NULL DEFAULT 0,
  question_text TEXT NOT NULL,
  question_type TEXT NOT NULL DEFAULT 'mcq' CHECK (question_type IN ('mcq')),
  marks INTEGER NOT NULL DEFAULT 1,
  options JSONB NOT NULL DEFAULT '[]',  -- ["Option A", "Option B", ...]
  correct_answer INTEGER NOT NULL,  -- index (0-based) of correct option
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Quiz assignments: links quiz to students (when teacher sends)
CREATE TABLE IF NOT EXISTS quiz_assignments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  quiz_id UUID NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
  student_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'submitted')),
  answers JSONB,  -- {"0": 1, "1": 2, ...} question_idx -> selected option idx
  score INTEGER,
  submitted_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(quiz_id, student_id)
);

CREATE INDEX IF NOT EXISTS idx_quizzes_teacher ON quizzes(teacher_id);
CREATE INDEX IF NOT EXISTS idx_quizzes_status ON quizzes(status);
CREATE INDEX IF NOT EXISTS idx_quiz_questions_quiz ON quiz_questions(quiz_id);
CREATE INDEX IF NOT EXISTS idx_quiz_assignments_student ON quiz_assignments(student_id);
CREATE INDEX IF NOT EXISTS idx_quiz_assignments_quiz ON quiz_assignments(quiz_id);

-- RLS (uses SECURITY DEFINER functions to avoid infinite recursion between quizzes <-> quiz_assignments)
ALTER TABLE quizzes ENABLE ROW LEVEL SECURITY;
ALTER TABLE quiz_questions ENABLE ROW LEVEL SECURITY;
ALTER TABLE quiz_assignments ENABLE ROW LEVEL SECURITY;

-- Helper: check if current user is assigned to quiz (bypasses RLS to prevent recursion)
CREATE OR REPLACE FUNCTION public.is_student_assigned_to_quiz(p_quiz_id UUID)
RETURNS BOOLEAN
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT EXISTS (
    SELECT 1 FROM quiz_assignments
    WHERE quiz_id = p_quiz_id AND student_id = auth.uid()::text
  );
$$;

-- Teachers can CRUD their own quizzes
CREATE POLICY "Teachers can CRUD own quizzes"
  ON quizzes FOR ALL
  USING (teacher_id = auth.uid()::text);

-- Students can read quizzes they are assigned to (uses function to avoid recursion)
CREATE POLICY "Students can read assigned quizzes"
  ON quizzes FOR SELECT
  USING (public.is_student_assigned_to_quiz(id));

-- Quiz questions: readable/editable by quiz owner
CREATE POLICY "Quiz owner can manage questions"
  ON quiz_questions FOR ALL
  USING (
    quiz_id IN (SELECT id FROM quizzes WHERE teacher_id = auth.uid()::text)
  );

-- Students can read questions of assigned quizzes (uses function to avoid recursion)
CREATE POLICY "Students can read questions of assigned quizzes"
  ON quiz_questions FOR SELECT
  USING (public.is_student_assigned_to_quiz(quiz_id));

-- Quiz assignments: teachers manage their quizzes
CREATE POLICY "Teachers can manage assignments for their quizzes"
  ON quiz_assignments FOR ALL
  USING (
    quiz_id IN (SELECT id FROM quizzes WHERE teacher_id = auth.uid()::text)
  );

-- Students can read and update their own assignments
CREATE POLICY "Students can read their own assignments"
  ON quiz_assignments FOR SELECT
  USING (student_id = auth.uid()::text);

CREATE POLICY "Students can update their own assignment"
  ON quiz_assignments FOR UPDATE
  USING (student_id = auth.uid()::text);
