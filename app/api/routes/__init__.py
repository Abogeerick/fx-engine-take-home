from app.api.routes.admin import router as admin_router
from app.api.routes.customers import router as customers_router
from app.api.routes.executions import router as executions_router
from app.api.routes.health import router as health_router
from app.api.routes.quotes import router as quotes_router

__all__ = [
    "admin_router",
    "customers_router",
    "executions_router",
    "health_router",
    "quotes_router",
]
