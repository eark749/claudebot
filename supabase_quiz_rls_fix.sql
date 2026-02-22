-- Fix RLS infinite recursion on quiz tables
-- Run this in Supabase SQL Editor if you already ran supabase_quiz_migration.sql and see error 42P17

-- Drop existing policies that cause recursion
DROP POLICY IF EXISTS "Students can read assigned quizzes" ON quizzes;
DROP POLICY IF EXISTS "Students can read questions of assigned quizzes" ON quiz_questions;
DROP POLICY IF EXISTS "Students can read and update their own assignments" ON quiz_assignments;
DROP POLICY IF EXISTS "Students can update their own assignment (submit)" ON quiz_assignments;

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

-- Recreate policies using the function
CREATE POLICY "Students can read assigned quizzes"
  ON quizzes FOR SELECT
  USING (public.is_student_assigned_to_quiz(id));

CREATE POLICY "Students can read questions of assigned quizzes"
  ON quiz_questions FOR SELECT
  USING (public.is_student_assigned_to_quiz(quiz_id));

CREATE POLICY "Students can read their own assignments"
  ON quiz_assignments FOR SELECT
  USING (student_id = auth.uid()::text);

CREATE POLICY "Students can update their own assignment"
  ON quiz_assignments FOR UPDATE
  USING (student_id = auth.uid()::text);
