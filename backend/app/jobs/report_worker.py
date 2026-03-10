"""
Celery worker: scheduled report generation.
"""

from app.jobs.celery_app import celery_app
from app.observability.logger import get_logger

logger = get_logger(__name__)


@celery_app.task(
    name="jobs.generate_report",
    queue="reports",
    max_retries=2,
    default_retry_delay=120,
)
def generate_monthly_report(tenant_id: str, month: str) -> None:
    """
    Generate a monthly financial summary report for a tenant.
    Uploads the PDF to S3 and sends a notification when ready.
    """
    logger.info("report_worker.started", tenant_id=tenant_id, month=month)

    # TODO: implement report generation (e.g., WeasyPrint / ReportLab)
    # 1. Query sales + expenses for the month
    # 2. Compute aggregates
    # 3. Render PDF template
    # 4. Upload to S3
    # 5. Create UploadedFile record
    # 6. Dispatch notification

    logger.info("report_worker.complete", tenant_id=tenant_id, month=month)
