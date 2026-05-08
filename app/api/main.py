"""FastAPI app factory + lifespan.

The lifespan handler owns the full composition root:
  * engine + session factory
  * Clock (SystemClock in production)
  * RateProvider (source + cache + breaker + singleflight)
  * RateRefreshScheduler (started on app startup, stopped on shutdown)

State is attached to ``app.state`` so route handlers can read it via
``request.app.state.<name>``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.errors import register_handlers
from app.api.middleware import correlation_id_middleware
from app.api.routes import (
    admin_router,
    customers_router,
    executions_router,
    health_router,
    quotes_router,
)
from app.domain.clock import SystemClock
from app.domain.currency import Currency
from app.infra.config import get_settings
from app.infra.db import make_engine, make_session_factory
from app.infra.rate_provider import (
    CircuitBreaker,
    ExchangeRatesApiSource,
    RateCache,
    RateProvider,
    RateRefreshScheduler,
    Singleflight,
)
from app.observability import configure_logging, get_logger

log = get_logger(__name__)


_DEFAULT_SCHEDULER_PAIRS: list[tuple[Currency, Currency]] = [
    (Currency.USD, Currency.KES),
    (Currency.USD, Currency.NGN),
    (Currency.USD, Currency.EUR),
    (Currency.EUR, Currency.KES),
    (Currency.EUR, Currency.NGN),
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()

    settings = get_settings()
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    clock = SystemClock()

    cache = RateCache(session_factory)
    breaker = CircuitBreaker(
        failure_threshold=3,
        cooldown=timedelta(seconds=30),
        clock=clock,
    )
    singleflight: Singleflight[tuple[Currency, Currency], None] = Singleflight(wait_timeout=5.0)
    source = ExchangeRatesApiSource(
        base_url="https://api.exchangeratesapi.io/v1",
        api_key=settings.rate_api_key,
        timeout=5.0,
    )
    rate_provider = RateProvider(
        source=source,
        cache=cache,
        circuit_breaker=breaker,
        singleflight=singleflight,
        clock=clock,
    )
    scheduler = RateRefreshScheduler(
        provider=rate_provider,
        pairs=_DEFAULT_SCHEDULER_PAIRS,
        interval=timedelta(seconds=60),
    )

    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.clock = clock
    app.state.rate_provider = rate_provider
    app.state.scheduler = scheduler
    app.state.settings = settings
    app.state.spread = Decimal("0.005")  # SPEC §5 default; configurable.

    await scheduler.start()
    log.info("app.startup", env=settings.env)

    try:
        yield
    finally:
        log.info("app.shutdown")
        await scheduler.stop()
        await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="Umba FX Engine", lifespan=lifespan)

    register_handlers(app)
    app.add_middleware(BaseHTTPMiddleware, dispatch=correlation_id_middleware)

    app.include_router(quotes_router)
    app.include_router(executions_router)
    app.include_router(customers_router)
    app.include_router(admin_router)
    app.include_router(health_router)

    return app


app = create_app()
