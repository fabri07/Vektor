"""Pydantic schemas for momentum engine endpoints."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel


class WeeklyHistoryItem(BaseModel):
    week_start: date
    week_end: date
    avg_score: float
    delta: float | None
    trend_label: str | None


class ActiveGoalResponse(BaseModel):
    weak_dimension: str
    goal: str
    action: str
    estimated_delta: int
    estimated_weeks: int


class MilestoneItem(BaseModel):
    code: str
    label: str
    unlocked_at: datetime


class MomentumProfileResponse(BaseModel):
    best_score_ever: int | None
    best_score_date: date | None
    active_goal: ActiveGoalResponse | None
    milestones_unlocked: list[MilestoneItem]
    estimated_value_protected_ars: float
    improving_streak_weeks: int
    weekly_history: list[WeeklyHistoryItem]
