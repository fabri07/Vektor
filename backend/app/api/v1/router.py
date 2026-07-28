"""Central v1 API router — aggregates all domain routers."""

from fastapi import APIRouter

from app.api.v1 import (
    access_requests,
    admin,
    agent,
    auth,
    automations,
    business_profiles,
    cash_closes,
    communication,
    contact,
    customers,
    economic_summary,
    expenses,
    fields,
    files,
    forecast,
    health_scores,
    ingestion,
    insights,
    integrations,
    marketing,
    momentum,
    notifications,
    oauth,
    onboarding,
    others,
    products,
    purchases,
    sales,
    settings,
    suppliers,
    tenants,
    users,
)

api_router = APIRouter()

api_router.include_router(admin.router, prefix="/admin", tags=["Admin"])
# Solicitudes de acceso: el formulario es público, la cola de revisión es SUPERADMIN.
api_router.include_router(
    access_requests.router, prefix="/access-requests", tags=["Access Requests"]
)
api_router.include_router(
    access_requests.admin_router,
    prefix="/admin/access-requests",
    tags=["Access Requests — Admin"],
)
api_router.include_router(agent.router, prefix="/agent", tags=["Agent"])
api_router.include_router(automations.router, prefix="/automations", tags=["Automations"])
api_router.include_router(auth.router, prefix="/auth", tags=["Auth"])
api_router.include_router(oauth.router, prefix="/auth/oauth", tags=["Auth — OAuth"])
api_router.include_router(tenants.router, prefix="/tenants", tags=["Tenants"])
api_router.include_router(users.router, prefix="/users", tags=["Users"])
api_router.include_router(
    business_profiles.router, prefix="/business-profiles", tags=["Business Profiles"]
)
api_router.include_router(sales.router, prefix="/sales", tags=["Sales"])
api_router.include_router(expenses.router, prefix="/expenses", tags=["Expenses"])
api_router.include_router(cash_closes.router, prefix="/cash-closes", tags=["Cash Closes"])
api_router.include_router(products.router, prefix="/products", tags=["Products"])
api_router.include_router(purchases.router, prefix="/purchases", tags=["Purchases"])
api_router.include_router(customers.router, prefix="/customers", tags=["Customers"])
api_router.include_router(suppliers.router, prefix="/suppliers", tags=["Suppliers"])
api_router.include_router(others.router, prefix="/others", tags=["Others"])
api_router.include_router(contact.router, prefix="/contact", tags=["Contact"])
api_router.include_router(
    communication.router, prefix="/communication", tags=["Communication"]
)
api_router.include_router(marketing.router, prefix="/marketing", tags=["Marketing"])
api_router.include_router(health_scores.router, prefix="/health-scores", tags=["Health Scores"])
api_router.include_router(insights.router, prefix="/insights", tags=["Insights"])
api_router.include_router(
    economic_summary.router, prefix="/economic-summary", tags=["Economic Summary"]
)
api_router.include_router(notifications.router, prefix="/notifications", tags=["Notifications"])
api_router.include_router(files.router, prefix="/files", tags=["Files"])
api_router.include_router(ingestion.router, prefix="/ingestion", tags=["Ingestion"])
api_router.include_router(integrations.router, prefix="/integrations", tags=["Integrations"])
api_router.include_router(onboarding.router, prefix="/onboarding", tags=["Onboarding"])
api_router.include_router(momentum.router, prefix="/momentum", tags=["Momentum"])
api_router.include_router(forecast.router, prefix="/forecast", tags=["Forecast"])
api_router.include_router(fields.router, prefix="/fields/definitions", tags=["Fields"])
api_router.include_router(settings.router, prefix="/settings", tags=["Settings"])
