"""25–40 resident capacity evidence without external LLM dependencies."""
import pytest

from scripts.resident_scale_harness import run_scale_scenario


@pytest.mark.anyio
@pytest.mark.parametrize("resident_count", [25, 40])
async def test_agent_loop_scale_targets_persist_every_tick(
    tmp_path,
    resident_count,
):
    report = await run_scale_scenario(
        resident_count,
        rounds=2,
        concurrency=8,
        database_path=tmp_path / f"scale-{resident_count}.db",
    )

    assert report.passed, report.invariant_failures
    assert report.expected_ticks == resident_count * 2
    assert report.attempted_ticks == report.expected_ticks
    assert sum(report.action_counts.values()) == report.expected_ticks
    assert report.unique_residents_ticked == resident_count
    assert report.db_resident_count == resident_count
    assert report.db_residents_updated == resident_count
    assert report.db_persisted_ticks == report.expected_ticks
    assert 0 < report.max_concurrency <= report.configured_concurrency
    assert report.external_llm_calls == 0
    assert not report.errors

    # Runtime is evidence, not a release threshold: shared CI hosts vary widely.
    assert report.elapsed_seconds >= 0


@pytest.mark.anyio
async def test_scale_harness_rejects_invalid_dimensions():
    with pytest.raises(ValueError, match="resident_count"):
        await run_scale_scenario(0)
    with pytest.raises(ValueError, match="rounds"):
        await run_scale_scenario(25, rounds=0)
    with pytest.raises(ValueError, match="concurrency"):
        await run_scale_scenario(25, concurrency=0)
