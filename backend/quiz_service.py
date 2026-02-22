"""OpenAI-powered quiz generation from document content."""

import json

from openai import OpenAI

from config import OPENAI_API_KEY


def generate_quiz(
    document_content: str,
    standard: int,
    total_marks: int,
    num_questions: int,
    document_name: str = "",
) -> list[dict]:
    """
    Generate MCQ quiz questions from document content using OpenAI.
    Returns list of dicts: {question_text, options, correct_answer, marks}
    """
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY is not set. Add it to your .env file.")

    client = OpenAI(api_key=OPENAI_API_KEY)

    age_guidance = {
        (1, 3): "6-8 years old - use very simple vocabulary, short sentences",
        (4, 6): "9-11 years old - clear accessible language, concrete examples",
        (7, 9): "12-14 years old - age-appropriate vocabulary, moderate complexity",
        (10, 12): "15-17 years old - high school level, can use sophisticated explanations",
    }
    age_desc = "appropriate for high school"
    for (lo, hi), desc in age_guidance.items():
        if lo <= standard <= hi:
            age_desc = desc
            break

    system_prompt = """You are an expert educational quiz creator. Given document content, create MCQ (multiple choice) questions.

Rules:
- Each question must have exactly 4 options (A, B, C, D).
- correct_answer is the 0-based index (0=first option, 1=second, etc).
- Distractors should be plausible but clearly wrong to someone who understood the content.
- Only derive questions from the provided document - do not add external knowledge.
- Return valid JSON matching the schema: {"questions": [{"question_text": "...", "options": ["A", "B", "C", "D"], "correct_answer": 0, "marks": 1}, ...]}
"""

    user_prompt = f"""Create a quiz for students of standard {standard} ({age_desc}).

Parameters:
- Number of questions: {num_questions}
- Total marks: {total_marks}
- Question type: MCQ only

Document name: {document_name or "Untitled"}
Document content:
---
{document_content[:12000]}
---

Generate exactly {num_questions} MCQ questions. Distribute marks so the total equals {total_marks}. Return valid JSON only, no markdown or extra text."""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.5,
    )

    content = response.choices[0].message.content
    if not content:
        raise ValueError("OpenAI returned empty response")

    data = json.loads(content)
    questions = data.get("questions", [])
    if not questions:
        raise ValueError("No questions in OpenAI response")

    # Validate and normalize
    result = []
    marks_so_far = 0
    per_q = max(1, total_marks // num_questions)
    remainder = total_marks - (per_q * num_questions)

    for i, q in enumerate(questions[:num_questions]):
        opts = q.get("options") or []
        if len(opts) < 2:
            opts = opts + ["Option B", "Option C", "Option D"][: 4 - len(opts)]
        opts = opts[:4]
        correct = int(q.get("correct_answer", 0))
        if correct < 0 or correct >= len(opts):
            correct = 0
        m = per_q + (1 if i < remainder else 0)
        result.append({
            "question_text": str(q.get("question_text", "")).strip() or f"Question {i + 1}",
            "options": opts,
            "correct_answer": correct,
            "marks": m,
        })
        marks_so_far += m

    return result
