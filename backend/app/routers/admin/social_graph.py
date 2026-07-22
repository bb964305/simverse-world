"""Admin social-graph endpoint (P2 §7.2) — require_admin on all routes.

Read-only JSON view of the resident relationship network: nodes (residents with
their stamped circle_id), edges (resident-resident familiarity/affinity), and
circles (connected components over strong ties). Back-end only — the frontend
visualization is intentionally out of scope this pass (PROGRESS)."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.routers.admin.middleware import require_admin
from app.services import circle_service

router = APIRouter(prefix="/social-graph", tags=["admin-social-graph"])


@router.get("")
async def social_graph(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Nodes + edges + circles of the resident relationship network."""
    return await circle_service.build_social_graph(db)
