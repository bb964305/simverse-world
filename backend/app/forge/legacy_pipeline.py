"""Legacy forge LLM generation pipelines (5-step guided + quick one-shot).

Serves the legacy /forge/answer (final step) and /forge/quick endpoints.
Superseded by app/forge/pipeline.py for new code. Moved verbatim from
app/services/forge_service.py (P1-6 file split).

Flow:
  1. start_forge()     -> create session, return Q2 (Q1 = name, already provided)
  2. submit_answer()   -> store answer, advance step, return next question
  3. After Q5 answer   -> trigger async LLM pipeline (this module)
  4. get_status()      -> poll session state (collecting | generating | done | error)
"""

import asyncio
import json
import random
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.forge.legacy_helpers import (
    _compute_star_rating_fallback,
    _extract_impression,
    _extract_role,
    _extract_text,
    _parse_combined_output,
)
from app.forge.legacy_prompts import (
    ABILITY_SYSTEM_PROMPT, ABILITY_USER_TEMPLATE,
    PERSONA_SYSTEM_PROMPT, PERSONA_USER_TEMPLATE,
    SOUL_SYSTEM_PROMPT, SOUL_USER_TEMPLATE,
    SCORING_SYSTEM_PROMPT, SCORING_USER_TEMPLATE,
    DISTRICT_SYSTEM_PROMPT, DISTRICT_USER_TEMPLATE,
    QUICK_EXTRACT_SYSTEM_PROMPT, QUICK_EXTRACT_USER_TEMPLATE,
)
from app.forge.legacy_sessions import _sessions
from app.forge.progress import (
    notify_forge_progress, notify_forge_done, notify_forge_error,
)
from app.llm.client import get_client
from app.llm.json_extract import extract_json_object
from app.llm.metering import record_usage
from app.models.resident import Resident
from app.services.civic_membership import UGC_RESIDENT_TYPE
from app.services.resident_placement import (
    SPRITE_KEYS,
    _generate_slug,
    allocate_resident_location,
    infer_location_id_from_text,
    normalize_location_id,
)


async def run_generation_pipeline(forge_id: str, db: AsyncSession) -> None:
    session = _sessions.get(forge_id)
    if not session:
        return

    try:
        name = session["name"]
        answers = session["answers"]
        ability_desc = answers.get("2", "")
        personality_desc = answers.get("3", "")
        soul_desc = answers.get("4", "")
        material = answers.get("5", "")
        if material.strip().lower() in ("跳过", "skip", "无", "没有", ""):
            material = "无补充材料"

        client = get_client()
        from app.config import settings as _settings
        model = _settings.effective_model

        # Generate ability.md
        await notify_forge_progress(session["user_id"], forge_id, "ability", "generating")
        ability_resp = await client.messages.create(
            model=model, max_tokens=1500, system=ABILITY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": ABILITY_USER_TEMPLATE.format(
                name=name, ability_description=ability_desc,
                personality_description=personality_desc, material=material,
            )}],
        )
        session["ability_md"] = _extract_text(ability_resp)
        await record_usage("forge_ability", model=model, owner="system", response=ability_resp)

        # Generate persona.md
        await notify_forge_progress(session["user_id"], forge_id, "persona", "generating")
        persona_resp = await client.messages.create(
            model=model, max_tokens=2000, system=PERSONA_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": PERSONA_USER_TEMPLATE.format(
                name=name, personality_description=personality_desc,
                ability_description=ability_desc, soul_description=soul_desc,
                material=material,
            )}],
        )
        session["persona_md"] = _extract_text(persona_resp)
        await record_usage("forge_persona", model=model, owner="system", response=persona_resp)

        # Generate soul.md
        await notify_forge_progress(session["user_id"], forge_id, "soul", "generating")
        soul_resp = await client.messages.create(
            model=model, max_tokens=1500, system=SOUL_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": SOUL_USER_TEMPLATE.format(
                name=name, soul_description=soul_desc,
                personality_description=personality_desc,
                ability_description=ability_desc, material=material,
            )}],
        )
        session["soul_md"] = _extract_text(soul_resp)
        await record_usage("forge_soul", model=model, owner="system", response=soul_resp)

        # Score quality
        await notify_forge_progress(session["user_id"], forge_id, "scoring", "generating")
        star_rating = await _score_quality(
            client, model, name,
            session["ability_md"], session["persona_md"], session["soul_md"],
        )
        session["star_rating"] = star_rating

        await notify_forge_progress(session["user_id"], forge_id, "placing", "generating")
        district, tile_x, tile_y, home_loc_id = await allocate_resident_location(
            db,
            requested_location_id=await _assign_district(client, model, name, ability_desc, personality_desc),
            ability_text=ability_desc,
            persona_text=personality_desc,
            soul_text=soul_desc,
        )
        session["district"] = district

        # Generate slug
        slug = _generate_slug(name)
        existing = await db.execute(select(Resident).where(Resident.slug == slug))
        if existing.scalar_one_or_none():
            slug = f"{slug}-{uuid.uuid4().hex[:6]}"

        # Compute SBTI personality
        from app.services.sbti_service import compute_sbti, update_meta_with_sbti
        forge_meta: dict = {
            "role": _extract_role(session["ability_md"]),
            "impression": _extract_impression(session["persona_md"]),
            "origin": "forge",
        }
        sbti = await compute_sbti(name, session["ability_md"], session["persona_md"], session["soul_md"])
        if sbti:
            forge_meta = update_meta_with_sbti(forge_meta, sbti)

        # Create resident — UGC type, no political rights (see civic_membership)
        resident = Resident(
            slug=slug, name=name, district=district, status="idle", heat=0,
            model_tier="standard", token_cost_per_turn=1, creator_id=session["user_id"],
            resident_type=UGC_RESIDENT_TYPE,
            ability_md=session["ability_md"], persona_md=session["persona_md"],
            soul_md=session["soul_md"],
            meta_json=forge_meta,
            sprite_key=random.choice(SPRITE_KEYS),
            tile_x=tile_x, tile_y=tile_y, star_rating=star_rating,
            home_location_id=home_loc_id,
        )
        db.add(resident)
        await db.commit()
        await db.refresh(resident)
        session["resident_id"] = resident.id

        # Reward creator
        from app.services.coin_service import reward
        await reward(db, session["user_id"], 50, "forge_creation")

        session["status"] = "done"
        await notify_forge_done(session["user_id"], forge_id)

    except Exception as e:
        session["status"] = "error"
        session["error"] = str(e)
        await notify_forge_error(session["user_id"], forge_id, str(e))


async def run_quick_pipeline(forge_id: str, db: AsyncSession) -> None:
    """
    Quick forge pipeline: single LLM call to extract all three layers from raw text.
    Much faster than the 5-step pipeline (1 call vs 5).
    """
    session = _sessions.get(forge_id)
    if not session:
        return

    try:
        name = session["name"]
        raw_text = session["answers"].get("2", "")
        await notify_forge_progress(session["user_id"], forge_id, "build", "generating")

        from app.config import settings as _settings
        import anthropic
        import logging

        model = _settings.effective_model
        user_msg = QUICK_EXTRACT_USER_TEMPLATE.format(name=name, raw_text=raw_text)
        logging.warning(f"[FORGE] LLM call starting for '{name}' ({len(raw_text)} chars)")

        # Run LLM call in a subprocess to isolate from uvicorn's event loop
        # (uvicorn's anyio backend breaks TLS handshake for external HTTPS calls in background tasks)
        import subprocess, sys, tempfile

        req_payload = json.dumps({
            "api_key": _settings.effective_api_key,
            "base_url": _settings.llm_base_url or "https://api.anthropic.com",
            "model": model,
            "max_tokens": 4096,
            "system": QUICK_EXTRACT_SYSTEM_PROMPT,
            "user_msg": user_msg,
        })

        # Write request to temp file (avoid shell escaping issues with Chinese text)
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write(req_payload)
            req_file = f.name

        script = f"""
import json, asyncio, sys

async def call_llm():
    with open('{req_file}') as f:
        req = json.load(f)
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=req['api_key'], base_url=req['base_url'])
    resp = await client.messages.create(
        model=req['model'], max_tokens=req['max_tokens'],
        system=req['system'],
        messages=[{{"role": "user", "content": req['user_msg']}}],
    )
    # Extract text block
    for block in resp.content:
        if hasattr(block, 'text'):
            print(block.text)
            return
    print('')

asyncio.run(call_llm())
"""

        logging.warning(f"[FORGE] Launching subprocess for LLM call...")
        proc = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                [sys.executable, '-c', script],
                capture_output=True, text=True, timeout=120,
                env={**__import__('os').environ, 'NO_PROXY': '*', 'HTTPS_PROXY': '', 'HTTP_PROXY': ''},
            ),
        )

        # Cleanup temp file
        import os
        os.unlink(req_file)

        if proc.returncode != 0:
            raise RuntimeError(f"LLM subprocess failed: {proc.stderr[:500]}")

        full_text = proc.stdout.strip()
        logging.warning(f"[FORGE] LLM returned {len(full_text)} chars")

        # Split on ===SPLIT===
        parts = [p.strip() for p in full_text.split("===SPLIT===")]
        session["ability_md"] = parts[0] if len(parts) > 0 else ""
        session["persona_md"] = parts[1] if len(parts) > 1 else ""
        session["soul_md"] = parts[2] if len(parts) > 2 else ""

        # If split didn't work well, try to parse by headers
        if len(parts) < 3 and "# 人格档案" in full_text:
            _parse_combined_output(session, full_text)

        # Score quality using fallback (skip LLM scoring for speed)
        session["star_rating"] = _compute_star_rating_fallback(
            session["ability_md"], session["persona_md"], session["soul_md"]
        )

        district, tile_x, tile_y, home_loc_id = await allocate_resident_location(
            db,
            ability_text=session["ability_md"],
            persona_text=session["persona_md"],
            soul_text=session["soul_md"],
        )
        session["district"] = district

        # Find tile, slug, create resident
        slug = _generate_slug(name)
        existing = await db.execute(select(Resident).where(Resident.slug == slug))
        if existing.scalar_one_or_none():
            slug = f"{slug}-{uuid.uuid4().hex[:6]}"

        # Compute SBTI personality
        from app.services.sbti_service import compute_sbti, update_meta_with_sbti
        quick_meta: dict = {
            "role": _extract_role(session["ability_md"]),
            "impression": _extract_impression(session["persona_md"]),
            "origin": "quick_forge",
        }
        sbti = await compute_sbti(name, session["ability_md"], session["persona_md"], session["soul_md"])
        if sbti:
            quick_meta = update_meta_with_sbti(quick_meta, sbti)

        # Quick path — same UGC type as the full path above.
        resident = Resident(
            slug=slug, name=name, district=district, status="idle", heat=0,
            model_tier="standard", token_cost_per_turn=1, creator_id=session["user_id"],
            resident_type=UGC_RESIDENT_TYPE,
            ability_md=session["ability_md"], persona_md=session["persona_md"],
            soul_md=session["soul_md"],
            meta_json=quick_meta,
            sprite_key=random.choice(SPRITE_KEYS),
            tile_x=tile_x, tile_y=tile_y, star_rating=session["star_rating"],
            home_location_id=home_loc_id,
        )
        db.add(resident)
        await db.commit()
        await db.refresh(resident)
        session["resident_id"] = resident.id

        from app.services.coin_service import reward
        await reward(db, session["user_id"], 50, "forge_creation")

        session["status"] = "done"
        await notify_forge_done(session["user_id"], forge_id)
        logging.info(f"Quick forge: '{name}' done — {district}, {session['star_rating']}★")

    except Exception as e:
        import logging, traceback
        logging.error(f"Quick forge error: {e}\n{traceback.format_exc()}")
        session["status"] = "error"
        session["error"] = str(e)
        await notify_forge_error(session["user_id"], forge_id, str(e))


async def _score_quality(client, model: str, name: str,
                         ability_md: str, persona_md: str, soul_md: str) -> int:
    try:
        resp = await client.messages.create(
            model=model, max_tokens=200, system=SCORING_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": SCORING_USER_TEMPLATE.format(
                name=name, ability_md=ability_md, persona_md=persona_md, soul_md=soul_md,
            )}],
        )
        text = _extract_text(resp).strip()
        data = extract_json_object(text)
        await record_usage(
            "forge_score", model=model, owner="system", response=resp,
            parse_ok=data is not None,
        )
        if data:
            return max(1, min(3, int(data.get("star_rating", 1))))
    except Exception:
        pass
    return _compute_star_rating_fallback(ability_md, persona_md, soul_md)


async def _assign_district(client, model: str, name: str,
                            ability_desc: str, personality_desc: str) -> str:
    try:
        resp = await client.messages.create(
            model=model, max_tokens=100, system=DISTRICT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": DISTRICT_USER_TEMPLATE.format(
                name=name, ability_description=ability_desc,
                personality_description=personality_desc,
            )}],
        )
        text = _extract_text(resp).strip()
        data = extract_json_object(text)
        await record_usage(
            "forge_district", model=model, owner="system", response=resp,
            parse_ok=data is not None,
        )
        if data:
            district = (
                data.get("location_id")
                or data.get("district")
                or data.get("placement")
            )
            return normalize_location_id(district, allocatable_only=True)
    except Exception:
        pass
    return infer_location_id_from_text(ability_desc, personality_desc)
