#!/usr/bin/env python3
"""Loopback-only Phaser review surface for a prepared static sprite batch."""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import html
import json
import os
import re
import secrets
import sys
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import parse_qs, unquote, urlsplit

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from PIL import Image

from app.services.resident_sprite_artifacts import (
    advance_stage,
    load_run,
    read_artifact,
    write_artifact,
    write_canonical_json_artifact,
)
from app.services.resident_sprite_batch import (
    ResidentSpriteBatchError,
    load_batch,
    sync_batch,
)
from app.services.resident_sprite_generation import (
    ResidentSpriteContractError,
    SanitizedError,
    canonical_json_bytes,
    validate_non_symlink_path,
    validate_run_id,
)
from scripts.generate_resident_sprite import ApprovalChecklist


DEFAULT_BATCH_ROOT = BACKEND_ROOT / "var/resident-sprite-batches"
DEFAULT_ATLAS = REPO_ROOT / "frontend/public/assets/village/agents/sprite.json"
DEFAULT_PHASER = REPO_ROOT / "frontend/node_modules/phaser/dist/phaser.min.js"
PHASER_VERSION = "3.90.0"
MAX_REQUEST_BYTES = 2 * 1024 * 1024
SCREENSHOT_SIZE = (640, 360)
REQUIRED_FRAMES = tuple(
    f"{direction}-walk.{index:03d}"
    for direction in ("down", "left", "right", "up")
    for index in range(3)
)
ASSET_KEY = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")


class ReviewError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_reviewer(value: str) -> str:
    if (
        value != value.strip()
        or not 1 <= len(value) <= 80
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ReviewError(HTTPStatus.BAD_REQUEST, "REVIEWER_INVALID", "reviewer must be canonical printable text")
    return value


def _decode_screenshot(value: Any) -> bytes:
    prefix = "data:image/png;base64,"
    if not isinstance(value, str) or not value.startswith(prefix) or len(value) > MAX_REQUEST_BYTES * 2:
        raise ReviewError(HTTPStatus.BAD_REQUEST, "SCREENSHOT_INVALID", "Phaser screenshot is missing or invalid")
    try:
        data = base64.b64decode(value[len(prefix):], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ReviewError(HTTPStatus.BAD_REQUEST, "SCREENSHOT_INVALID", "Phaser screenshot is invalid") from exc
    if not data or len(data) > MAX_REQUEST_BYTES:
        raise ReviewError(HTTPStatus.BAD_REQUEST, "SCREENSHOT_INVALID", "Phaser screenshot has an invalid size")
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            if image.format != "PNG" or image.size != SCREENSHOT_SIZE or image.mode not in {"RGB", "RGBA"}:
                raise ValueError
    except Exception as exc:
        raise ReviewError(HTTPStatus.BAD_REQUEST, "SCREENSHOT_INVALID", "Phaser screenshot has invalid pixels or dimensions") from exc
    return data


def _find_item(batch: Any, asset_key: str) -> Any:
    if not ASSET_KEY.fullmatch(asset_key):
        raise ReviewError(HTTPStatus.NOT_FOUND, "BATCH_ITEM_UNKNOWN", "batch item was not found")
    item = next((candidate for candidate in batch.items if candidate.asset_key == asset_key), None)
    if item is None or item.run_id is None:
        raise ReviewError(HTTPStatus.NOT_FOUND, "BATCH_ITEM_UNKNOWN", "batch item has no generation run")
    return item


def _validate_render_payload(payload: Any, *, run_id: str, texture_sha256: str) -> tuple[bytes, dict[str, Any]]:
    if not isinstance(payload, dict) or set(payload) != {
        "checklist", "notes", "phaser_version", "frames", "texture_sha256", "screenshot_data_url"
    }:
        raise ReviewError(HTTPStatus.BAD_REQUEST, "REVIEW_PAYLOAD_INVALID", "review payload has unexpected fields")
    frames = payload["frames"]
    if not isinstance(frames, list) or tuple(frames) != REQUIRED_FRAMES:
        raise ReviewError(HTTPStatus.BAD_REQUEST, "PHASER_FRAMES_INVALID", "Phaser did not confirm the exact required frame set")
    if payload["phaser_version"] != PHASER_VERSION or payload["texture_sha256"] != texture_sha256:
        raise ReviewError(HTTPStatus.BAD_REQUEST, "PHASER_RENDER_MISMATCH", "Phaser render evidence does not match the candidate")
    notes = payload["notes"]
    if not isinstance(notes, str) or notes != notes.strip() or len(notes) > 1000:
        raise ReviewError(HTTPStatus.BAD_REQUEST, "REVIEW_NOTES_INVALID", "review notes are invalid")
    screenshot = _decode_screenshot(payload["screenshot_data_url"])
    evidence = {
        "schema_version": 1,
        "run_id": run_id,
        "review_surface": "phaser-canvas-v1",
        "phaser_version": PHASER_VERSION,
        "texture_sha256": texture_sha256,
        "frames": list(REQUIRED_FRAMES),
        "canvas_width": SCREENSHOT_SIZE[0],
        "canvas_height": SCREENSHOT_SIZE[1],
        "screenshot_sha256": _sha256(screenshot),
    }
    return screenshot, evidence


def approve_item(
    *, batch_root: Path, batch_id: str, artifact_root: Path, asset_key: str,
    reviewer: str, payload: Any,
) -> dict[str, Any]:
    reviewer = _canonical_reviewer(reviewer)
    batch = sync_batch(batch_root, batch_id, artifact_root)
    item = _find_item(batch, asset_key)
    run = load_run(artifact_root, item.run_id)
    if run.state not in {"auto_qc_passed", "candidate_ready", "phaser_reviewed", "human_approved"}:
        raise ReviewError(HTTPStatus.CONFLICT, "REVIEW_STATE_INVALID", "candidate is not ready for approval")
    texture = read_artifact(artifact_root, item.run_id, "candidate/texture.png")
    screenshot, submitted_render = _validate_render_payload(
        payload, run_id=item.run_id, texture_sha256=_sha256(texture)
    )
    checklist = ApprovalChecklist.model_validate(
        {
            "schema_version": 1,
            "run_id": item.run_id,
            "decision": "approve",
            "checks": payload["checklist"],
            "notes": payload["notes"],
        }
    )
    if not all(checklist.checks.model_dump().values()):
        raise ReviewError(HTTPStatus.BAD_REQUEST, "APPROVAL_INVALID", "all nine review checks are required")
    try:
        stored_screenshot = read_artifact(
            artifact_root, item.run_id, "review/phaser-screenshot.png"
        )
    except ResidentSpriteContractError:
        stored_screenshot = screenshot
        write_artifact(
            artifact_root, item.run_id, "review/phaser-screenshot.png", stored_screenshot
        )
    render = {
        **submitted_render,
        "screenshot_sha256": _sha256(stored_screenshot),
    }
    try:
        evidence = json.loads(
            read_artifact(artifact_root, item.run_id, "review/phaser.json")
        )
    except ResidentSpriteContractError:
        evidence = {
            "schema_version": 2,
            "run_id": item.run_id,
            "reviewer": reviewer,
            "reviewed_at": datetime.now(timezone.utc),
            "render": render,
        }
        write_canonical_json_artifact(
            artifact_root, item.run_id, "review/phaser.json", evidence
        )
    if (
        not isinstance(evidence, dict)
        or evidence.get("schema_version") != 2
        or evidence.get("run_id") != item.run_id
        or evidence.get("reviewer") != reviewer
        or evidence.get("render") != render
    ):
        raise ReviewError(
            HTTPStatus.CONFLICT,
            "REVIEW_EVIDENCE_CONFLICT",
            "stored Phaser review evidence differs from the candidate",
        )
    if run.state == "human_approved":
        return {"asset_key": asset_key, "run_id": item.run_id, "state": run.state}
    if run.state == "auto_qc_passed":
        run = advance_stage(artifact_root, item.run_id, stage="candidate", state="candidate_ready")
    if run.state == "candidate_ready":
        run = advance_stage(artifact_root, item.run_id, stage="phaser_review", state="phaser_reviewed")
    if run.state != "phaser_reviewed":
        raise ReviewError(HTTPStatus.CONFLICT, "APPROVAL_STATE_INVALID", "candidate is not ready for approval")
    write_canonical_json_artifact(
        artifact_root,
        item.run_id,
        "review/approval.json",
        {
            **checklist.model_dump(mode="json"),
            "reviewer": reviewer,
            "approved_at": datetime.now(timezone.utc),
        },
    )
    run = advance_stage(
        artifact_root, item.run_id, stage="human_approval", state="human_approved"
    )
    sync_batch(batch_root, batch_id, artifact_root)
    return {
        "asset_key": asset_key,
        "run_id": item.run_id,
        "state": run.state,
        "reviewer": reviewer,
    }


def reject_item(
    *, batch_root: Path, batch_id: str, artifact_root: Path, asset_key: str,
    reviewer: str, reason: Any,
) -> dict[str, Any]:
    reviewer = _canonical_reviewer(reviewer)
    if not isinstance(reason, str) or reason != reason.strip() or not 1 <= len(reason) <= 1000:
        raise ReviewError(HTTPStatus.BAD_REQUEST, "REJECTION_REASON_INVALID", "rejection reason is invalid")
    batch = sync_batch(batch_root, batch_id, artifact_root)
    item = _find_item(batch, asset_key)
    run = load_run(artifact_root, item.run_id)
    if run.state == "quarantined":
        return {"asset_key": asset_key, "run_id": item.run_id, "state": run.state}
    if run.state not in {"auto_qc_passed", "candidate_ready", "phaser_reviewed"}:
        raise ReviewError(HTTPStatus.CONFLICT, "REJECTION_STATE_INVALID", "candidate is not ready for rejection")
    write_canonical_json_artifact(
        artifact_root,
        item.run_id,
        "review/rejection.json",
        {
            "schema_version": 1,
            "run_id": item.run_id,
            "reviewer": reviewer,
            "reason": reason,
            "rejected_at": datetime.now(timezone.utc),
        },
    )
    run = advance_stage(
        artifact_root,
        item.run_id,
        stage="quarantine",
        state="quarantined",
        error=SanitizedError(
            code="HUMAN_REJECTED",
            message="candidate was rejected during human review",
        ),
    )
    sync_batch(batch_root, batch_id, artifact_root)
    return {"asset_key": asset_key, "run_id": item.run_id, "state": run.state}


class ReviewContext:
    def __init__(
        self,
        *,
        batch_root: Path,
        batch_id: str,
        artifact_root: Path,
        reviewer: str,
        token: str,
        atlas_path: Path,
        phaser_path: Path,
        origin: str,
    ) -> None:
        self.batch_root = batch_root
        self.batch_id = batch_id
        self.artifact_root = artifact_root
        self.reviewer = reviewer
        self.token = token
        self.atlas_path = atlas_path
        self.phaser_path = phaser_path
        self.origin = origin


def _batch_payload(context: ReviewContext) -> dict[str, Any]:
    batch = sync_batch(context.batch_root, context.batch_id, context.artifact_root)
    items = []
    for item in batch.items:
        request = json.loads(
            (context.batch_root / context.batch_id / item.request_file).read_text(encoding="utf-8")
        )
        items.append(
            {
                "asset_key": item.asset_key,
                "sprite_key": item.sprite_key,
                "display_name": request["display_name"],
                "appearance": request["appearance"],
                "run_id": item.run_id,
                "state": item.run_state,
                "request_count": item.submitted_request_count,
                "texture_sha256": (
                    _sha256(read_artifact(context.artifact_root, item.run_id, "candidate/texture.png"))
                    if item.run_id is not None and item.run_state in {
                        "auto_qc_passed", "candidate_ready", "phaser_reviewed", "human_approved"
                    }
                    else None
                ),
            }
        )
    return {"batch_id": batch.batch_id, "reviewer": context.reviewer, "items": items}


def _html(token: str) -> bytes:
    token_json = json.dumps(token)
    frames_json = json.dumps(REQUIRED_FRAMES)
    version_json = json.dumps(PHASER_VERSION)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>居民形象批次审核</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;font:14px system-ui,sans-serif;background:#15181d;color:#eef1f4;height:100vh;overflow:hidden}}
main{{display:grid;grid-template-columns:280px minmax(0,1fr);height:100vh}}aside{{border-right:1px solid #343a43;overflow:auto;background:#1b1f25}}
header{{position:sticky;top:0;padding:14px 16px;background:#1b1f25;border-bottom:1px solid #343a43;z-index:2}}h1{{font-size:16px;margin:0 0 4px}}#summary{{font-size:12px;color:#9ba5b1}}
#items{{list-style:none;padding:0;margin:0}}#items button{{width:100%;border:0;border-bottom:1px solid #2d333b;background:transparent;color:inherit;text-align:left;padding:10px 14px;cursor:pointer}}
#items button:hover,#items button.active{{background:#262c34}}.slot{{font-weight:650}}.meta{{font-size:11px;color:#9ba5b1;margin-top:3px}}.state{{float:right;color:#6ee7b7}}
section{{min-width:0;overflow:auto;padding:20px 24px}}.toolbar{{display:flex;align-items:start;justify-content:space-between;gap:16px;max-width:920px}}.toolbar>div:first-child{{min-width:0}}h2{{font-size:20px;margin:0 0 6px}}#appearance{{color:#b7c0ca;line-height:1.55;max-width:720px}}
#canvas{{width:min(640px,100%);height:auto;aspect-ratio:16/9;margin:18px 0;border:1px solid #3c444f;background:#20242b;image-rendering:pixelated}}#canvas canvas{{display:block;width:100%!important;height:100%!important}}#render-status{{font-size:12px;color:#fbbf24;white-space:nowrap}}
fieldset{{border:0;border-top:1px solid #343a43;margin:18px 0 0;padding:16px 0;display:grid;grid-template-columns:repeat(3,minmax(180px,1fr));gap:10px;max-width:920px}}
label{{display:flex;gap:8px;align-items:flex-start}}textarea{{width:min(920px,100%);height:74px;background:#1c2026;border:1px solid #3c444f;color:#fff;padding:9px;resize:vertical}}
.actions{{display:flex;gap:10px;margin-top:12px}}button.action{{border:1px solid #4b5563;background:#2a3038;color:#fff;padding:8px 13px;cursor:pointer}}button.approve{{background:#166534;border-color:#22c55e}}button.reject{{background:#7f1d1d;border-color:#ef4444}}button:disabled{{opacity:.4;cursor:not-allowed}}
#message{{margin-top:10px;min-height:20px;color:#fbbf24}}@media(max-width:800px){{main{{grid-template-columns:minmax(0,1fr)}}aside{{display:none}}section{{padding:14px}}.toolbar{{display:block}}#render-status{{margin-top:8px}}fieldset{{grid-template-columns:1fr}}}}
</style></head><body><main><aside><header><h1>居民形象批次审核</h1><div id="summary"></div></header><ul id="items"></ul></aside>
<section><div class="toolbar"><div><h2 id="title">选择候选</h2><div id="appearance"></div></div><div id="render-status">尚未渲染</div></div><div id="canvas"></div>
<fieldset id="checks">
<label><input type="checkbox" data-key="identity_consistent">四方向身份与服装一致</label><label><input type="checkbox" data-key="directions_correct">方向朝向正确</label><label><input type="checkbox" data-key="gait_readable">三帧步态清晰</label>
<label><input type="checkbox" data-key="scale_and_baseline_stable">尺寸与脚底基线稳定</label><label><input type="checkbox" data-key="anatomy_and_clipping_clean">肢体与裁切干净</label><label><input type="checkbox" data-key="background_clean">透明背景无残边</label>
<label><input type="checkbox" data-key="asymmetry_acceptable">镜像与非对称细节可接受</label><label><input type="checkbox" data-key="originality_acceptable">原创性可接受</label><label><input type="checkbox" data-key="gameplay_fit">游戏内辨识度合格</label>
</fieldset><textarea id="notes" maxlength="1000" placeholder="审核备注"></textarea><div class="actions"><button class="action approve" id="approve" disabled>批准</button><button class="action reject" id="reject" disabled>拒绝</button></div><div id="message"></div>
</section></main><script src="/phaser.min.js?token={html.escape(token)}"></script><script>
const TOKEN={token_json}, REQUIRED_FRAMES={frames_json}, PHASER_VERSION={version_json};let batch,current,game,renderReady=false;
const q=(s)=>document.querySelector(s), all=(s)=>[...document.querySelectorAll(s)];
async function api(path,options={{}}){{const separator=path.includes('?')?'&':'?';const response=await fetch(path+separator+'token='+encodeURIComponent(TOKEN),options);const data=await response.json();if(!response.ok)throw new Error(data.error?.message||'请求失败');return data}}
function resetChecks(){{all('#checks input').forEach(x=>x.checked=false);q('#notes').value='';q('#message').textContent=''}}
function updateActions(){{const reviewable=current&&['auto_qc_passed','candidate_ready','phaser_reviewed'].includes(current.state);q('#approve').disabled=!reviewable||!renderReady||!all('#checks input').every(x=>x.checked);q('#reject').disabled=!reviewable}}
function renderList(){{q('#summary').textContent=`批次 ${{batch.batch_id.slice(0,8)}} · 审核人 ${{batch.reviewer}}`;q('#items').replaceChildren();for(const item of batch.items){{const li=document.createElement('li'),b=document.createElement('button'),state=document.createElement('span'),slot=document.createElement('span'),meta=document.createElement('div');state.className='state';state.textContent=item.state;slot.className='slot';slot.textContent=item.sprite_key;meta.className='meta';meta.textContent=`${{item.asset_key}} · ${{item.request_count}} requests`;b.append(state,slot,meta);b.className=current?.asset_key===item.asset_key?'active':'';b.onclick=()=>select(item);li.append(b);q('#items').append(li)}}}}
async function select(item){{current=item;renderReady=false;resetChecks();renderList();q('#title').textContent=item.display_name;q('#appearance').textContent=item.appearance;q('#render-status').textContent='Phaser 加载中';if(game){{game.destroy(true);game=null}}q('#canvas').innerHTML='';updateActions();if(!item.texture_sha256){{q('#render-status').textContent='候选尚未就绪';return}}
game=new Phaser.Game({{type:Phaser.CANVAS,width:640,height:360,parent:'canvas',backgroundColor:'#20242b',pixelArt:true,render:{{antialias:false}},scene:{{preload(){{this.load.atlas('candidate',`/candidate/${{item.run_id}}/texture.png?token=${{encodeURIComponent(TOKEN)}}`,`/atlas.json?token=${{encodeURIComponent(TOKEN)}}`)}},create(){{const missing=REQUIRED_FRAMES.filter(f=>!this.textures.get('candidate').has(f));if(missing.length){{q('#render-status').textContent='缺少帧: '+missing.join(', ');return}}const dirs=['down','left','right','up'],labels=['下','左','右','上'];dirs.forEach((dir,i)=>{{const key='walk-'+dir,idle=`${{dir}}-walk.001`;this.anims.create({{key,frames:[0,1,2].map(n=>({{key:'candidate',frame:`${{dir}}-walk.${{String(n).padStart(3,'0')}}`}})),frameRate:6,repeat:-1}});this.add.sprite(104+i*144,168,'candidate',idle).setData('idleFrame',idle).setScale(3).play(key);this.add.text(96+i*144,250,labels[i],{{fontFamily:'sans-serif',fontSize:'15px',color:'#dce3ea'}})}});this.add.text(18,18,`${{item.display_name}} · ${{item.run_id.slice(0,8)}}`,{{fontFamily:'monospace',fontSize:'13px',color:'#9ba5b1'}});renderReady=true;q('#render-status').textContent=`Phaser ${{Phaser.VERSION}} · 12 帧已验证`;updateActions()}}}}}})}}
all('#checks input').forEach(x=>x.onchange=updateActions);
q('#approve').onclick=async()=>{{if(!current||!game||!renderReady)return;q('#message').textContent='正在固化审核证据…';const checklist=Object.fromEntries(all('#checks input').map(x=>[x.dataset.key,x.checked]));try{{for(const object of game.scene.getScenes(true)[0].children.list){{if(object instanceof Phaser.GameObjects.Sprite&&object.getData('idleFrame')){{object.anims.stop();object.setFrame(object.getData('idleFrame'))}}}}await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));const result=await api(`/api/items/${{current.asset_key}}/approve`,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{checklist,notes:q('#notes').value.trim(),phaser_version:Phaser.VERSION,frames:REQUIRED_FRAMES,texture_sha256:current.texture_sha256,screenshot_data_url:game.canvas.toDataURL('image/png')}})}});q('#message').textContent=`已批准 ${{result.asset_key}}`;await load();await select(batch.items.find(x=>x.asset_key===current.asset_key))}}catch(e){{q('#message').textContent=e.message}}}};
q('#reject').onclick=async()=>{{if(!current)return;const reason=q('#notes').value.trim();if(!reason){{q('#message').textContent='拒绝必须填写原因';return}}try{{const result=await api(`/api/items/${{current.asset_key}}/reject`,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{reason}})}});q('#message').textContent=`已拒绝 ${{result.asset_key}}`;await load();await select(batch.items.find(x=>x.asset_key===current.asset_key))}}catch(e){{q('#message').textContent=e.message}}}};
async function load(){{batch=await api('/api/batch');renderList()}}load().then(()=>{{const first=batch.items.find(x=>x.texture_sha256);if(first)select(first)}}).catch(e=>q('#message').textContent=e.message);
</script></body></html>""".encode("utf-8")


def handler_for(context: ReviewContext):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SimverseSpriteReview/1"

        def log_message(self, format: str, *args: Any) -> None:
            # Request targets contain the one-time review token.
            return

        def _send(self, status: int, data: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'",
            )
            self.end_headers()
            self.wfile.write(data)

        def _error(self, error: Exception) -> None:
            if isinstance(error, ReviewError):
                status, code, message = error.status, error.code, str(error)
            elif isinstance(error, (ResidentSpriteBatchError, ResidentSpriteContractError)):
                status, code, message = HTTPStatus.CONFLICT, error.code, str(error)
            else:
                status, code, message = HTTPStatus.INTERNAL_SERVER_ERROR, "REVIEW_SERVER_ERROR", "review operation failed"
            self._send(status, canonical_json_bytes({"error": {"code": code, "message": message}}), "application/json")

        def _authorized_path(self) -> tuple[str, dict[str, list[str]]]:
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            supplied = query.get("token")
            if (
                supplied is None
                or len(supplied) != 1
                or not secrets.compare_digest(supplied[0], context.token)
            ):
                raise ReviewError(HTTPStatus.FORBIDDEN, "REVIEW_TOKEN_INVALID", "review token is invalid")
            return unquote(parsed.path), query

        def do_GET(self) -> None:
            try:
                path, _ = self._authorized_path()
                if path == "/":
                    self._send(HTTPStatus.OK, _html(context.token), "text/html; charset=utf-8")
                elif path == "/api/batch":
                    self._send(HTTPStatus.OK, canonical_json_bytes(_batch_payload(context)), "application/json")
                elif path == "/atlas.json":
                    self._send(HTTPStatus.OK, context.atlas_path.read_bytes(), "application/json")
                elif path == "/phaser.min.js":
                    self._send(HTTPStatus.OK, context.phaser_path.read_bytes(), "text/javascript; charset=utf-8")
                elif path.startswith("/candidate/") and path.endswith("/texture.png"):
                    run_id = path.removeprefix("/candidate/").removesuffix("/texture.png")
                    validate_run_id(run_id)
                    batch = load_batch(context.batch_root, context.batch_id)
                    if run_id not in {item.run_id for item in batch.items}:
                        raise ReviewError(HTTPStatus.NOT_FOUND, "CANDIDATE_NOT_FOUND", "candidate was not found")
                    self._send(HTTPStatus.OK, read_artifact(context.artifact_root, run_id, "candidate/texture.png"), "image/png")
                else:
                    raise ReviewError(HTTPStatus.NOT_FOUND, "NOT_FOUND", "review resource was not found")
            except Exception as exc:
                self._error(exc)

        def do_POST(self) -> None:
            try:
                path, _ = self._authorized_path()
                if self.headers.get("Origin") != context.origin:
                    raise ReviewError(HTTPStatus.FORBIDDEN, "REVIEW_ORIGIN_INVALID", "review request origin is invalid")
                if self.headers.get_content_type() != "application/json":
                    raise ReviewError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "CONTENT_TYPE_INVALID", "JSON content type is required")
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError as exc:
                    raise ReviewError(HTTPStatus.BAD_REQUEST, "CONTENT_LENGTH_INVALID", "content length is invalid") from exc
                if not 1 <= length <= MAX_REQUEST_BYTES * 2:
                    raise ReviewError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "REVIEW_PAYLOAD_TOO_LARGE", "review payload is too large")
                payload = json.loads(self.rfile.read(length))
                match = re.fullmatch(r"/api/items/([^/]+)/(approve|reject)", path)
                if match is None:
                    raise ReviewError(HTTPStatus.NOT_FOUND, "NOT_FOUND", "review resource was not found")
                asset_key, action = match.groups()
                if action == "approve":
                    result = approve_item(
                        batch_root=context.batch_root, batch_id=context.batch_id,
                        artifact_root=context.artifact_root, asset_key=asset_key,
                        reviewer=context.reviewer, payload=payload,
                    )
                else:
                    if not isinstance(payload, dict) or set(payload) != {"reason"}:
                        raise ReviewError(HTTPStatus.BAD_REQUEST, "REVIEW_PAYLOAD_INVALID", "rejection payload is invalid")
                    result = reject_item(
                        batch_root=context.batch_root, batch_id=context.batch_id,
                        artifact_root=context.artifact_root, asset_key=asset_key,
                        reviewer=context.reviewer, reason=payload["reason"],
                    )
                self._send(HTTPStatus.OK, canonical_json_bytes(result), "application/json")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._error(ReviewError(HTTPStatus.BAD_REQUEST, "JSON_INVALID", "request JSON is invalid"))
            except Exception as exc:
                self._error(exc)

    return Handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve a loopback-only Phaser review surface")
    parser.add_argument("--batch-root", type=Path, default=DEFAULT_BATCH_ROOT)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--atlas", type=Path, default=DEFAULT_ATLAS)
    parser.add_argument("--phaser", type=Path, default=DEFAULT_PHASER)
    parser.add_argument("--port", type=int, default=4187)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_run_id(args.batch_id)
        reviewer = _canonical_reviewer(args.reviewer)
        if not 1024 <= args.port <= 65535:
            raise ReviewError(HTTPStatus.BAD_REQUEST, "PORT_INVALID", "port must be between 1024 and 65535")
        atlas_path = validate_non_symlink_path(args.atlas, must_exist=True)
        phaser_path = validate_non_symlink_path(args.phaser, must_exist=True)
        expected_atlas_root = validate_non_symlink_path(DEFAULT_ATLAS.parent, must_exist=True)
        expected_phaser_root = validate_non_symlink_path(DEFAULT_PHASER.parent, must_exist=True)
        if (
            not atlas_path.is_file()
            or not phaser_path.is_file()
            or atlas_path.parent != expected_atlas_root
            or atlas_path.name != "sprite.json"
            or phaser_path.parent != expected_phaser_root
            or phaser_path.name != "phaser.min.js"
            or phaser_path.stat().st_size > 2 * 1024 * 1024
        ):
            raise ReviewError(HTTPStatus.BAD_REQUEST, "REVIEW_ASSET_MISSING", "atlas or local Phaser bundle is missing")
        load_batch(args.batch_root, args.batch_id)
        token = secrets.token_urlsafe(32)
        origin = f"http://127.0.0.1:{args.port}"
        context = ReviewContext(
            batch_root=args.batch_root,
            batch_id=args.batch_id,
            artifact_root=args.artifact_root,
            reviewer=reviewer,
            token=token,
            atlas_path=atlas_path,
            phaser_path=phaser_path,
            origin=origin,
        )
        server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(context))
        server.daemon_threads = True
        print(f"Resident sprite review: {origin}/?token={token}", flush=True)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    except (ReviewError, ResidentSpriteBatchError, ResidentSpriteContractError) as exc:
        code = exc.code
        sys.stderr.buffer.write(canonical_json_bytes({"error": {"code": code, "message": str(exc)}}) + b"\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
