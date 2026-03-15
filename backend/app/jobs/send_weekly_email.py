"""
Celery task: send weekly email summary to all tenant owners.

Runs after update_momentum_all_tenants (Monday 11:30 UTC / 08:30 ART).
Gathers: business name, health score + delta, primary risk, active goal,
priority action, value protected.
Sends HTML email via SMTPClient and records a notification row (channel='email').
"""

from __future__ import annotations

import asyncio
import uuid as _uuid
from decimal import Decimal
from typing import Any

from app.jobs.celery_app import celery_app
from app.observability.logger import get_logger

logger = get_logger(__name__)


# ── Email HTML template ────────────────────────────────────────────────────────

def _build_html(
    business_name: str,
    score: int,
    delta: int | None,
    risk_title: str | None,
    goal_text: str | None,
    action_text: str | None,
    value_protected_ars: Decimal,
    dashboard_url: str,
) -> str:
    def _format_ars(v: Decimal) -> str:
        return f"$ {int(v):,}".replace(",", ".")

    delta_html = ""
    if delta is not None and delta > 0:
        delta_html = f'<span style="color:#10b981;font-size:14px;">↑ +{delta} vs semana pasada</span>'
    elif delta is not None and delta < 0:
        delta_html = f'<span style="color:#ef4444;font-size:14px;">↓ {delta} vs semana pasada</span>'
    else:
        delta_html = '<span style="color:#9ca3af;font-size:14px;">→ Sin cambios</span>'

    risk_row = ""
    if risk_title:
        risk_row = f"""
        <tr>
          <td style="padding:12px 0;border-bottom:1px solid #f3f4f6;">
            <p style="margin:0 0 4px 0;font-size:11px;color:#9ca3af;text-transform:uppercase;letter-spacing:0.08em;">Riesgo principal</p>
            <p style="margin:0;font-size:14px;color:#374151;">{risk_title}</p>
          </td>
        </tr>"""

    goal_row = ""
    if goal_text:
        goal_row = f"""
        <tr>
          <td style="padding:12px 0;border-bottom:1px solid #f3f4f6;">
            <p style="margin:0 0 4px 0;font-size:11px;color:#9ca3af;text-transform:uppercase;letter-spacing:0.08em;">Meta activa</p>
            <p style="margin:0;font-size:14px;color:#374151;">{goal_text}</p>
          </td>
        </tr>"""

    action_row = ""
    if action_text:
        action_row = f"""
        <tr>
          <td style="padding:12px 0;border-bottom:1px solid #f3f4f6;">
            <p style="margin:0 0 4px 0;font-size:11px;color:#9ca3af;text-transform:uppercase;letter-spacing:0.08em;">Acción prioritaria</p>
            <p style="margin:0;font-size:14px;color:#374151;">{action_text}</p>
          </td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background-color:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f9fafb;padding:40px 0;">
    <tr>
      <td align="center">
        <table width="560" cellpadding="0" cellspacing="0" style="background-color:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e5e7eb;">

          <!-- Header -->
          <tr>
            <td style="background-color:#1A1A2E;padding:28px 32px;">
              <p style="margin:0;font-size:22px;font-weight:700;color:#ffffff;letter-spacing:-0.02em;">Véktor</p>
              <p style="margin:4px 0 0 0;font-size:13px;color:rgba(255,255,255,0.5);">Resumen semanal · {business_name}</p>
            </td>
          </tr>

          <!-- Score block -->
          <tr>
            <td style="padding:32px 32px 0 32px;">
              <p style="margin:0 0 4px 0;font-size:11px;color:#9ca3af;text-transform:uppercase;letter-spacing:0.08em;">Health Score</p>
              <div style="display:flex;align-items:baseline;gap:8px;">
                <span style="font-size:56px;font-weight:700;color:#1A1A2E;line-height:1;">{score}</span>
                <span style="font-size:20px;color:#d1d5db;font-weight:300;">/ 100</span>
              </div>
              <div style="margin-top:6px;">{delta_html}</div>
            </td>
          </tr>

          <!-- Details table -->
          <tr>
            <td style="padding:20px 32px 0 32px;">
              <table width="100%" cellpadding="0" cellspacing="0">
                {risk_row}
                {goal_row}
                {action_row}
                <tr>
                  <td style="padding:12px 0;">
                    <p style="margin:0 0 4px 0;font-size:11px;color:#9ca3af;text-transform:uppercase;letter-spacing:0.08em;">Valor protegido acumulado</p>
                    <p style="margin:0;font-size:20px;font-weight:700;color:#1A1A2E;">{_format_ars(value_protected_ars)}</p>
                    <p style="margin:2px 0 0 0;font-size:11px;color:#9ca3af;">Estimación basada en mejoras de margen y caja.</p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- CTA -->
          <tr>
            <td style="padding:28px 32px 32px 32px;">
              <a href="{dashboard_url}"
                 style="display:inline-block;background-color:#E63946;color:#ffffff;font-size:14px;font-weight:600;
                        text-decoration:none;padding:12px 24px;border-radius:8px;letter-spacing:0.01em;">
                Ver mi dashboard →
              </a>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="padding:20px 32px;border-top:1px solid #f3f4f6;">
              <p style="margin:0;font-size:11px;color:#9ca3af;">
                Este resumen se genera automáticamente cada lunes. Para dejar de recibirlo, configurá tus preferencias en el dashboard.
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _build_plain(
    business_name: str,
    score: int,
    delta: int | None,
    risk_title: str | None,
    goal_text: str | None,
    action_text: str | None,
    value_protected_ars: Decimal,
) -> str:
    delta_str = ""
    if delta is not None and delta > 0:
        delta_str = f" (↑ +{delta} vs semana pasada)"
    elif delta is not None and delta < 0:
        delta_str = f" (↓ {delta} vs semana pasada)"

    lines = [
        f"Resumen semanal — {business_name}",
        "",
        f"Health Score: {score}/100{delta_str}",
    ]
    if risk_title:
        lines.append(f"Riesgo principal: {risk_title}")
    if goal_text:
        lines.append(f"Meta activa: {goal_text}")
    if action_text:
        lines.append(f"Acción prioritaria: {action_text}")
    lines.append(f"Valor protegido acumulado: $ {int(value_protected_ars):,}".replace(",", "."))
    return "\n".join(lines)


# ── Data gathering ─────────────────────────────────────────────────────────────

async def _gather_email_data(
    tenant_id: _uuid.UUID,
    session: Any,
) -> dict[str, Any] | None:
    """Return dict with all data needed for the email, or None if tenant not ready."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.persistence.models.business import BusinessProfile, MomentumProfile  # noqa: PLC0415
    from app.persistence.models.score import HealthScoreSnapshot, WeeklyScoreHistory  # noqa: PLC0415
    from app.persistence.models.tenant import Tenant  # noqa: PLC0415
    from app.persistence.models.user import User  # noqa: PLC0415
    from app.persistence.models.business import Insight  # noqa: PLC0415

    # Tenant + business name
    tenant_row = await session.get(Tenant, tenant_id)
    if not tenant_row:
        return None

    business_name = tenant_row.display_name

    # Latest health score
    hs_result = await session.execute(
        select(HealthScoreSnapshot)
        .where(HealthScoreSnapshot.tenant_id == tenant_id)
        .order_by(HealthScoreSnapshot.snapshot_date.desc())
        .limit(1)
    )
    hs = hs_result.scalar_one_or_none()
    if not hs:
        return None

    score = int(hs.total_score)

    # Delta from latest weekly history entry
    wh_result = await session.execute(
        select(WeeklyScoreHistory)
        .where(WeeklyScoreHistory.tenant_id == tenant_id)
        .order_by(WeeklyScoreHistory.week_start.desc())
        .limit(1)
    )
    wh = wh_result.scalar_one_or_none()
    delta: int | None = int(wh.delta) if (wh and wh.delta is not None) else None

    # Primary risk from latest insight
    insight_result = await session.execute(
        select(Insight)
        .where(Insight.tenant_id == tenant_id)
        .order_by(Insight.created_at.desc())
        .limit(1)
    )
    insight = insight_result.scalar_one_or_none()
    risk_title: str | None = insight.title if insight else None

    # Active goal from momentum
    momentum_result = await session.execute(
        select(MomentumProfile).where(MomentumProfile.tenant_id == tenant_id)
    )
    momentum = momentum_result.scalar_one_or_none()
    goal_text: str | None = None
    action_text: str | None = None
    value_protected_ars = Decimal("0")
    if momentum:
        if momentum.active_goal_json:
            goal_text = momentum.active_goal_json.get("goal")
            action_text = momentum.active_goal_json.get("action")
        value_protected_ars = momentum.estimated_value_protected_ars or Decimal("0")

    # Owner email(s)
    users_result = await session.execute(
        select(User).where(
            User.tenant_id == tenant_id,
            User.role_code == "OWNER",
            User.is_active.is_(True),
        )
    )
    owner_users = list(users_result.scalars().all())

    return {
        "business_name": business_name,
        "score": score,
        "delta": delta,
        "risk_title": risk_title,
        "goal_text": goal_text,
        "action_text": action_text,
        "value_protected_ars": value_protected_ars,
        "owner_users": owner_users,
        "tenant_id": str(tenant_id),
    }


# ── Celery task ────────────────────────────────────────────────────────────────

@celery_app.task(  # type: ignore[misc]
    name="jobs.send_weekly_email_summary",
    queue="notifications",
    max_retries=2,
    default_retry_delay=300,
)
def send_weekly_email_summary(tenant_id: str) -> None:
    """Send the weekly email summary for a single tenant."""
    asyncio.run(_async_send(tenant_id))


async def _async_send(tenant_id: str) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    from app.config.settings import get_settings  # noqa: PLC0415
    from app.persistence.models.notification import Notification  # noqa: PLC0415

    s = get_settings()
    engine = create_async_engine(s.DATABASE_URL, pool_pre_ping=True)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)  # type: ignore[call-overload]

    async with factory() as session:
        tid = _uuid.UUID(tenant_id)
        data = await _gather_email_data(tid, session)
        if not data:
            logger.warning("send_weekly_email.no_data", tenant_id=tenant_id)
            await engine.dispose()
            return

        dashboard_url = f"{s.FRONTEND_URL}/dashboard" if hasattr(s, "FRONTEND_URL") else "https://app.vektor.com.ar/dashboard"

        html = _build_html(
            business_name=data["business_name"],
            score=data["score"],
            delta=data["delta"],
            risk_title=data["risk_title"],
            goal_text=data["goal_text"],
            action_text=data["action_text"],
            value_protected_ars=data["value_protected_ars"],
            dashboard_url=dashboard_url,
        )
        plain = _build_plain(
            business_name=data["business_name"],
            score=data["score"],
            delta=data["delta"],
            risk_title=data["risk_title"],
            goal_text=data["goal_text"],
            action_text=data["action_text"],
            value_protected_ars=data["value_protected_ars"],
        )
        subject = f"Tu resumen semanal — {data['business_name']} | Score: {data['score']}"

        if s.ENABLE_EMAIL_NOTIFICATIONS:
            from app.integrations.smtp import SMTPClient  # noqa: PLC0415

            smtp = SMTPClient()
            for user in data["owner_users"]:
                smtp.send(
                    to_email=user.email,
                    subject=subject,
                    body_html=html,
                    body_text=plain,
                )

        # Record notification row per owner user
        for user in data["owner_users"]:
            notification = Notification(
                tenant_id=tid,
                user_id=user.user_id,
                title=subject,
                body=f"Score: {data['score']}/100. Revisá tu dashboard para el detalle completo.",
                notification_type="weekly_summary",
                channel="email",
                is_read=False,
                metadata_={
                    "score": data["score"],
                    "delta": data["delta"],
                    "delivery_status": "sent" if s.ENABLE_EMAIL_NOTIFICATIONS else "disabled",
                },
            )
            session.add(notification)

        await session.commit()

    await engine.dispose()
    logger.info("send_weekly_email.done", tenant_id=tenant_id)


# ── Fan-out task (all tenants) ─────────────────────────────────────────────────

@celery_app.task(  # type: ignore[misc]
    name="jobs.send_weekly_email_all_tenants",
    queue="notifications",
)
def send_weekly_email_all_tenants() -> None:
    """Fan-out: enqueue send_weekly_email_summary for every active tenant."""
    asyncio.run(_fan_out())


async def _fan_out() -> None:
    from sqlalchemy import select  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    from app.config.settings import get_settings  # noqa: PLC0415
    from app.persistence.models.tenant import Tenant  # noqa: PLC0415

    s = get_settings()
    engine = create_async_engine(s.DATABASE_URL, pool_pre_ping=True)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)  # type: ignore[call-overload]

    async with factory() as session:
        result = await session.execute(
            select(Tenant.tenant_id).where(Tenant.status == "ACTIVE")
        )
        tenant_ids = [str(row) for row in result.scalars().all()]

    await engine.dispose()

    for tid in tenant_ids:
        send_weekly_email_summary.delay(tid)

    logger.info("send_weekly_email.fan_out", count=len(tenant_ids))
