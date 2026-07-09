import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.routers import auth, users, residents, forge, profile, search, bulletin, onboarding, sprites, avatar, settings as settings_router, media as media_router, events as events_router, notifications as notifications_router, achievements as achievements_router, shop as shop_router
# Import the achievement checkers so their @on(...) handlers register on the bus.
import app.events.achievements  # noqa: F401
from app.routers.admin import router as admin_router
from app.ws.handlers import websocket_handler
from app.tasks.heat_cron import heat_cron_loop
from app.tasks.event_cron import event_cron_loop
from app.tasks.embedding_backfill import embedding_backfill_loop
from app.agent.loop import agent_loop
from app.http import close_client
from app.redis_client import close_redis
from app.ws.manager import manager

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app):
    # Auto-create tables — dev convenience only, off by default (P0-6).
    # Production schema is managed exclusively by Alembic migrations.
    if settings.auto_create_tables:
        from app.database import engine, Base
        # Import all models so Base.metadata knows about them
        import app.models.user  # noqa: F401
        import app.models.resident  # noqa: F401
        import app.models.conversation  # noqa: F401
        import app.models.transaction  # noqa: F401
        import app.models.system_config  # noqa: F401
        import app.models.forge_session  # noqa: F401
        import app.models.pending_message  # noqa: F401
        import app.models.memory  # noqa: F401
        import app.models.personality_history  # noqa: F401
        import app.models.world_event  # noqa: F401
        import app.models.notification  # noqa: F401
        import app.models.achievement  # noqa: F401
        import app.models.shop  # noqa: F401
        import app.models.location_visit  # noqa: F401
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # Seed achievement definitions (idempotent) so GET /achievements + the
        # ops-editable table are populated in dev. Fail-open: a seed hiccup must
        # never block startup.
        try:
            from app.database import async_session
            from app.events.achievements import seed_achievements
            async with async_session() as _db:
                await seed_achievements(_db)
        except Exception:
            logger.warning("achievement seed skipped", exc_info=True)

    # WS pub/sub subscriber (P0-3b): every API worker relays broadcast/direct
    # envelopes from Redis to its own local sockets. Runs regardless of
    # run_background_tasks — this process owns live WebSocket clients even when
    # the agent loops live in the standalone worker.
    subscriber_task = asyncio.create_task(manager.run_subscriber())

    # S5: the location-visit consumer runs on every API worker (move messages
    # arrive on the worker that owns the user's socket), independent of
    # run_background_tasks. DB writes happen off the move hot path here.
    from app.services.location_tracker import location_consumer_loop
    location_task = asyncio.create_task(location_consumer_loop())

    # Background loops run in-process only in single-process mode (P0-3):
    # with RUN_BACKGROUND_TASKS=false they are owned by the standalone
    # agent-worker process (python -m app.agent.main).
    background_tasks: list[asyncio.Task] = []
    if settings.run_background_tasks:
        background_tasks = [
            asyncio.create_task(heat_cron_loop()),
            asyncio.create_task(event_cron_loop()),
            asyncio.create_task(agent_loop.run()),
            asyncio.create_task(embedding_backfill_loop()),
        ]
        logger.info("Background loops started in-process (run_background_tasks=true)")
    else:
        logger.info(
            "run_background_tasks=false — background loops are delegated "
            "to the agent-worker process"
        )
    yield
    subscriber_task.cancel()
    location_task.cancel()
    for task in background_tasks:
        task.cancel()
    await close_client()
    await close_redis()


app = FastAPI(title="Simverse World API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- REST rate limiting (OPTIMIZATION_PLAN P1-1, limit sub-item) ---
# The Limiter instance lives in app.rate_limit so routers can import the
# decorator without a circular dependency on this module. Here we only wire
# it into the app + register the 429 handler.
from app.rate_limit import limiter as _rest_limiter  # noqa: E402
from slowapi import _rate_limit_exceeded_handler  # noqa: E402
from slowapi.errors import RateLimitExceeded  # noqa: E402

app.state.limiter = _rest_limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(residents.router)
app.include_router(forge.router)
app.include_router(profile.router)
app.include_router(search.router)
app.include_router(bulletin.router)
app.include_router(onboarding.router)
app.include_router(sprites.router)
app.include_router(avatar.router)
app.include_router(settings_router.router)
app.include_router(media_router.router)
app.include_router(events_router.router)
app.include_router(notifications_router.router)
app.include_router(achievements_router.router)
app.include_router(shop_router.router)
app.include_router(admin_router)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket_handler(websocket)


@app.get("/health")
async def health():
    return {"status": "ok"}
