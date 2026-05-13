from __future__ import annotations

import random
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from anticheatbot.db.models import GroupSettings, QuizQuestion, VerifyQuizSession


class QuizError(RuntimeError):
    pass


@dataclass
class DrawnQuiz:
    questions: list[QuizQuestion]


async def total_quiz_points(session: AsyncSession, chat_id: int) -> int:
    stmt = select(func.coalesce(func.sum(QuizQuestion.points), 0)).where(QuizQuestion.chat_id == chat_id)
    return int((await session.execute(stmt)).scalar_one())


async def draw_quiz_for_token(
    session: AsyncSession,
    *,
    chat_id: int,
    token: str,
    gs: GroupSettings,
) -> DrawnQuiz:
    stmt = select(QuizQuestion).where(QuizQuestion.chat_id == chat_id)
    all_q = list((await session.scalars(stmt)).all())
    if len(all_q) < gs.quiz_draw_count:
        msg = f"题库题目不足：需要至少 {gs.quiz_draw_count} 题"
        raise QuizError(msg)
    total = sum(q.points for q in all_q)
    if total != 100:
        raise QuizError(f"题库分值总和必须为 100，当前为 {total}")

    picked = random.sample(all_q, k=gs.quiz_draw_count)
    ids = [q.id for q in picked]
    session.add(VerifyQuizSession(token=token, question_ids_json=ids))
    await session.flush()
    return DrawnQuiz(questions=picked)


async def grade_answers(
    session: AsyncSession,
    *,
    token: str,
    answers: dict[int, int],
) -> tuple[int, int]:
    """Return (score, max_score)."""
    sess = await session.get(VerifyQuizSession, token)
    if sess is None:
        raise QuizError("quiz session missing")
    ids = list(sess.question_ids_json)
    score = 0
    max_score = 0
    for qid in ids:
        q = await session.get(QuizQuestion, qid)
        if q is None:
            continue
        max_score += q.points
        ans = answers.get(qid)
        if ans is None:
            continue
        if ans == q.correct_index:
            score += q.points
    return score, max_score
