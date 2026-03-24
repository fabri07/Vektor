"""Central v1 API router — aggregates all domain routers."""

from fastapi import APIRouter

from app.api.v1 import (
    admin,
    auth,
    business_profiles,
    expenses,
    files,
    health_scores,
    ingestion,
    insights,
    momentum,
    notifications,
    onboarding,
    products,
    sales,
    tenants,
    users,
)

api_router = APIRouter()

api_router.include_router(admin.router, prefix="/admin", tags=["Admin"])
api_router.include_router(auth.router, prefix="/auth", tags=["Auth"])
api_router.include_router(tenants.router, prefix="/tenants", tags=["Tenants"])
api_router.include_router(users.router, prefix="/users", tags=["Users"])
api_router.include_router(
    business_profiles.router, prefix="/business-profiles", tags=["Business Profiles"]
)
api_router.include_router(sales.router, prefix="/sales", tags=["Sales"])
api_router.include_router(expenses.router, prefix="/expenses", tags=["Expenses"])
api_router.include_router(products.router, prefix="/products", tags=["Products"])
api_router.include_router(
    health_scores.router, prefix="/health-scores", tags=["Health Scores"]
)
api_router.include_router(insights.router, prefix="/insights", tags=["Insights"])
api_router.include_router(
    notifications.router, prefix="/notifications", tags=["Notifications"]
)
api_router.include_router(files.router, prefix="/files", tags=["Files"])
api_router.include_router(ingestion.router, prefix="/ingestion", tags=["Ingestion"])
api_router.include_router(onboarding.router, prefix="/onboarding", tags=["Onboarding"])
api_router.include_router(momentum.router, prefix="/momentum", tags=["Momentum"])
