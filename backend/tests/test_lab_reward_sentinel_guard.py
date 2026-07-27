"""lab_reward: settlement_splits guarded the creator payout with a bare
literal ``creator_id != "system"`` — seed NPCs carry creator_id =
SYSTEM_CREATOR_ID (a UUID), which slips through and mints the lab-task
creator's bps share straight into the sentinel account. Lab line is off by
default, so this is the lowest-urgency of the four sentinel-minting sites,
but it shares the same defect.
"""
import pytest

from app.models.lab_task import LabTask
from app.models.resident import Resident
from app.services import lab_terminalization_service
from app.services.system_users import SYSTEM_CREATOR_ID


@pytest.mark.anyio
async def test_settlement_splits_excludes_sentinel_creator(db_session):
    db_session.add(Resident(
        slug="sentinel-lab", name="P", creator_id=SYSTEM_CREATOR_ID,
        district="cafe", status="idle", tile_x=1, tile_y=1,
    ))
    await db_session.commit()

    task = LabTask(
        id="lab-task-sentinel", researcher_slug="sentinel-lab",
        reward_sc=100, platform_fee_sc=0, terminal_creator_share_bps=5000,
    )

    splits = await lab_terminalization_service.settlement_splits(db_session, task)

    assert all(recipient != SYSTEM_CREATOR_ID for recipient, _, _ in splits), \
        "lab_reward split must never target the sentinel creator"
    assert ("treasury:sentinel-lab", 100, "lab_treasury:lab-task-sentinel") in splits, \
        "the whole reward must fold into the treasury split when the creator is a sentinel"


@pytest.mark.anyio
async def test_settlement_splits_still_pays_real_creator(db_session):
    """Guard rail: a real (non-sentinel) creator must still get paid — this
    would catch an overzealous fix that drops the creator split entirely."""
    db_session.add(Resident(
        slug="real-lab", name="P", creator_id="real-creator-1",
        district="cafe", status="idle", tile_x=1, tile_y=1,
    ))
    await db_session.commit()

    task = LabTask(
        id="lab-task-real", researcher_slug="real-lab",
        reward_sc=100, platform_fee_sc=0, terminal_creator_share_bps=5000,
    )

    splits = await lab_terminalization_service.settlement_splits(db_session, task)

    assert ("real-creator-1", 50, "lab_reward:lab-task-real") in splits
    assert ("treasury:real-lab", 50, "lab_treasury:lab-task-real") in splits
