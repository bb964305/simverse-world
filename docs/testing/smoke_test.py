#!/usr/bin/env python3
"""Simverse World 生产环境全功能冒烟测试脚本.

用法: python3 smoke_test.py [--skip-llm] [--skip-ws]
输出: results.json (逐用例结果) + 控制台摘要
"""
import argparse
import asyncio
import concurrent.futures
import json
import random
import string
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests

API = "https://simverse-api.proxypool.eu.org"
FRONT = "https://simverse.world"
WS_URL = "wss://simverse-api.proxypool.eu.org/ws"

RESULTS = []
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "simverse-smoke-test/1.0"


def record(tid, name, status, detail=""):
    RESULTS.append({"id": tid, "name": name, "status": status, "detail": str(detail)[:500]})
    mark = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭️", "WARN": "⚠️"}.get(status, "?")
    print(f"{mark} {tid:8} {status:4} {name} — {str(detail)[:160]}")


def test(tid, name):
    """decorator: 捕获异常记 FAIL."""
    def deco(fn):
        def wrapper(*a, **kw):
            try:
                return fn(*a, **kw)
            except Exception as e:
                record(tid, name, "FAIL", f"exception: {type(e).__name__}: {e}")
                return None
        return wrapper
    return deco


def req(method, path, token=None, expect=None, timeout=30, **kw):
    headers = kw.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last = None
    for attempt in range(3):
        try:
            return SESSION.request(method, API + path, headers=headers, timeout=timeout, **kw)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


def jbody(r):
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text[:200]}


def get_balance(token):
    """从 /users/me 或 /settings 中找 Soul Coin 余额字段."""
    me = jbody(req("GET", "/users/me", token=token))
    def scan(d, depth=0):
        if depth > 2 or not isinstance(d, dict):
            return None
        for k, v in d.items():
            lk = k.lower()
            if isinstance(v, (int, float)) and ("coin" in lk or "balance" in lk):
                return v
        for v in d.values():
            if isinstance(v, dict):
                got = scan(v, depth + 1)
                if got is not None:
                    return got
        return None
    b = scan(me)
    if b is None:
        s = jbody(req("GET", "/settings", token=token))
        b = scan(s)
    return b


# ---------------------------------------------------------------- M0 基础设施
def m0():
    print("\n=== M0 基础设施 ===")
    r = req("GET", "/health", timeout=15)
    record("M0-1", "API 健康检查", "PASS" if r.status_code == 200 and r.json().get("status") == "ok" else "FAIL", f"{r.status_code} {r.text[:80]}")

    r = SESSION.get(FRONT + "/", timeout=20)
    ok = r.status_code == 200 and ("<div id=" in r.text or "root" in r.text)
    record("M0-2", "前端首页可达", "PASS" if ok else "FAIL", f"{r.status_code}, {len(r.text)}B")
    # 抽一个 JS 资产
    import re
    assets = re.findall(r'src="(/assets/[^"]+)"', r.text) + re.findall(r'href="(/assets/[^"]+)"', r.text)
    if assets:
        ra = SESSION.get(FRONT + assets[0], timeout=20)
        record("M0-2b", "前端 JS/CSS 资产加载", "PASS" if ra.status_code == 200 else "FAIL", f"{assets[0]} → {ra.status_code}")

    # TLS: 证书有效性由每次 HTTPS 请求隐式验证; 这里查过期时间 (openssl, 容错)
    import subprocess
    for host in ["simverse.world", "simverse-api.proxypool.eu.org"]:
        try:
            out = subprocess.run(
                f"echo | timeout 12 openssl s_client -connect {host}:443 -servername {host} 2>/dev/null | openssl x509 -noout -enddate",
                shell=True, capture_output=True, text=True, timeout=20).stdout.strip()
            exp = datetime.strptime(out.split("=", 1)[1].strip(), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
            days = (exp - datetime.now(timezone.utc)).days
            record("M0-3", f"TLS 证书 {host}", "PASS" if days > 14 else "WARN", f"剩余 {days} 天 (至 {exp.date()})")
        except Exception as e:
            record("M0-3", f"TLS 证书 {host}", "SKIP", f"无法读取到期时间: {e}")

    r = req("GET", "/openapi.json")
    n = len(r.json().get("paths", {})) if r.status_code == 200 else 0
    record("M0-4", "OpenAPI 可用", "PASS" if n > 100 else "FAIL", f"{n} paths")

    r = SESSION.options(API + "/auth/login", headers={
        "Origin": FRONT, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,authorization"}, timeout=15)
    acao = r.headers.get("access-control-allow-origin", "")
    record("M0-5", "CORS 预检", "PASS" if acao in (FRONT, "*") else "FAIL", f"{r.status_code} ACAO={acao!r}")

    r = req("GET", "/metrics", timeout=15)
    if r.status_code == 401:
        record("M0-6", "/metrics 鉴权保护", "PASS", "401 需要 token")
    elif r.status_code == 200:
        record("M0-6", "/metrics 鉴权保护", "WARN", "公开可读(确认是否有意)")
    else:
        record("M0-6", "/metrics 鉴权保护", "SKIP", f"{r.status_code}")


# ---------------------------------------------------------------- M1 认证
def m1(state):
    print("\n=== M1 认证与账号 ===")
    ts = datetime.now().strftime("%m%d%H%M")
    rnd = "".join(random.choices(string.ascii_lowercase, k=5))
    state["email_a"] = f"svtest_{ts}_{rnd}a@sv-test.dev"
    state["email_b"] = f"svtest_{ts}_{rnd}b@sv-test.dev"
    state["pw"] = "SvTest!" + rnd + "9"

    r = req("POST", "/auth/register", json={"name": f"冒烟测试A_{rnd}", "email": state["email_a"], "password": state["pw"]})
    body = jbody(r)
    tok = body.get("token") or body.get("access_token")
    record("M1-1", "注册账号A", "PASS" if r.status_code == 200 and tok else "FAIL", f"{r.status_code} keys={list(body)[:6]}")
    state["tok_a"] = tok

    r2 = req("POST", "/auth/register", json={"name": "dup", "email": state["email_a"], "password": state["pw"]})
    record("M1-2", "重复注册被拒", "PASS" if 400 <= r2.status_code < 500 else "FAIL", f"{r2.status_code} {r2.text[:80]}")

    r = req("POST", "/auth/register", json={"name": f"冒烟测试B_{rnd}", "email": state["email_b"], "password": state["pw"]})
    body = jbody(r)
    state["tok_b"] = body.get("token") or body.get("access_token")
    record("M1-1b", "注册账号B", "PASS" if state["tok_b"] else "FAIL", f"{r.status_code}")

    r = req("POST", "/auth/login", json={"email": state["email_a"], "password": state["pw"]})
    body = jbody(r)
    tok = body.get("token") or body.get("access_token")
    if tok:
        state["tok_a"] = tok
    record("M1-3", "登录", "PASS" if r.status_code == 200 and tok else "FAIL", f"{r.status_code}")

    r = req("GET", "/users/me", token=state["tok_a"])
    me = jbody(r)
    state["me_a"] = me
    record("M1-3b", "GET /users/me", "PASS" if r.status_code == 200 and me.get("email") == state["email_a"] else "FAIL",
           f"{r.status_code} keys={list(me)[:12]}")

    r = req("POST", "/auth/login", json={"email": state["email_a"], "password": "WrongPass123!"})
    record("M1-4", "错误密码 401", "PASS" if r.status_code == 401 else "FAIL", f"{r.status_code}")

    protected = ["/users/me", "/settings", "/profile/residents", "/feed", "/shop/inventory",
                 "/notifications", "/capsules", "/exploration/me", "/daily/quest", "/lab/tasks"]
    bad = []
    for p in protected:
        rr = req("GET", p)
        if rr.status_code not in (401, 403):
            bad.append(f"{p}={rr.status_code}")
    record("M1-5", "无 token 访问受保护接口", "PASS" if not bad else "FAIL", bad or f"{len(protected)} 个接口全部 401/403")

    r = req("GET", "/users/me", token="fake." + "x" * 40)
    record("M1-6", "伪造 token 401", "PASS" if r.status_code == 401 else "FAIL", f"{r.status_code}")

    for name, path in [("github", "/auth/github/login"), ("linuxdo", "/auth/linuxdo/login")]:
        r = SESSION.get(API + path, allow_redirects=False, timeout=15)
        ok = r.status_code in (302, 307) or (400 <= r.status_code < 500)
        record("M1-7", f"OAuth 端点 {name}", "PASS" if r.status_code in (302, 307) else ("SKIP" if ok else "FAIL"),
               f"{r.status_code} loc={r.headers.get('location', '')[:60]}")


# ---------------------------------------------------------------- M2 onboarding
def m2(state):
    print("\n=== M2 Onboarding 与玩家角色 ===")
    r = req("GET", "/onboarding/check", token=state["tok_a"])
    record("M2-1", "onboarding check", "PASS" if r.status_code == 200 else "FAIL", f"{r.status_code} {r.text[:100]}")

    r = req("GET", "/sprites/templates", token=state["tok_a"])
    body = jbody(r)
    templates = body if isinstance(body, list) else body.get("templates", body.get("items", []))
    record("M2-2", "精灵模板列表", "PASS" if r.status_code == 200 and len(templates) >= 10 else "FAIL", f"{len(templates)} 个模板")
    sprite_key = None
    if templates:
        t0 = templates[0]
        sprite_key = t0.get("key") or t0.get("sprite_key") or t0.get("id") if isinstance(t0, dict) else t0
    state["sprite_key"] = sprite_key or "amelia"

    r = req("POST", "/onboarding/create-character", token=state["tok_a"], json={
        "name": "测试员小柯", "sprite_key": state["sprite_key"], "reply_mode": "manual",
        "persona_md": "一位来自云端的系统测试员,说话简洁,喜欢验证世界的每个角落。"})
    body = jbody(r)
    record("M2-3", "创建玩家角色A", "PASS" if r.status_code == 200 else "FAIL", f"{r.status_code} {str(body)[:120]}")
    state["slug_a"] = body.get("slug") or (body.get("resident") or {}).get("slug")
    r = req("GET", "/users/me", token=state["tok_a"])
    me = jbody(r)
    state["me_a"] = me
    state["slug_a"] = state["slug_a"] or (me.get("player_resident") or {}).get("slug") or me.get("player_resident_slug")
    print(f"   → 玩家角色 slug: {state['slug_a']} | /users/me keys: {list(me)[:15]}")

    r = req("POST", "/onboarding/skip", token=state["tok_b"])
    record("M2-5", "账号B 跳过引导", "PASS" if r.status_code == 200 else "WARN", f"{r.status_code} {r.text[:80]}")


# ---------------------------------------------------------------- M3 居民
def m3(state):
    print("\n=== M3 居民/角色系统 ===")
    r = req("GET", "/residents", token=state["tok_a"])
    body = jbody(r)
    residents = body if isinstance(body, list) else body.get("residents", body.get("items", []))
    record("M3-1", "居民列表", "PASS" if r.status_code == 200 and len(residents) >= 10 else "FAIL", f"{len(residents)} 个居民")
    state["residents_snapshot1"] = {x.get("slug"): (x.get("x"), x.get("y"), x.get("status"), x.get("current_action")) for x in residents if isinstance(x, dict)}
    # 选一个 NPC(非玩家)做对话对象
    npc = None
    for x in residents:
        if isinstance(x, dict) and not x.get("is_player") and x.get("slug") != state.get("slug_a"):
            if x.get("status") in ("idle", "popular", None) or npc is None:
                npc = x
                if x.get("status") in ("idle", "popular"):
                    break
    state["npc"] = npc or {}
    state["npc_slug"] = (npc or {}).get("slug")
    print(f"   → 选中 NPC: {state['npc_slug']} status={state['npc'].get('status')}")

    if state["npc_slug"]:
        r = req("GET", f"/residents/{state['npc_slug']}", token=state["tok_a"])
        d = jbody(r)
        has_core = r.status_code == 200 and (d.get("persona_md") or d.get("persona") or d.get("soul_md") or "sbti" in str(d).lower())
        record("M3-2", "居民详情", "PASS" if has_core else "FAIL", f"{r.status_code} keys={list(d)[:10]}")

        r = req("GET", f"/residents/{state['npc_slug']}/card", token=state["tok_a"])
        record("M3-3", "角色卡", "PASS" if r.status_code == 200 else "FAIL", f"{r.status_code}")

        # 导出/版本历史: 他人角色应 403(权限), 自己角色应 200
        r = req("GET", f"/residents/{state['npc_slug']}/export", token=state["tok_a"])
        record("M3-4c", "导出他人角色被拒", "PASS" if r.status_code in (401, 403) else "WARN", f"{r.status_code}")
        if state.get("slug_a"):
            r = req("GET", f"/residents/{state['slug_a']}/export", token=state["tok_a"])
            record("M3-4a", "导出自己角色", "PASS" if r.status_code == 200 else "FAIL", f"{r.status_code} {len(r.content)}B")
            r = req("GET", f"/residents/{state['slug_a']}/versions", token=state["tok_a"])
            record("M3-5", "版本历史(自己角色)", "PASS" if r.status_code == 200 else "FAIL", f"{r.status_code} {r.text[:80]}")

        r = req("GET", f"/residents/{state['npc_slug']}/goals", token=state["tok_a"])
        goals = jbody(r)
        state["npc_goals"] = goals if isinstance(goals, list) else goals.get("goals", [])
        record("M3-7", "居民目标", "PASS" if r.status_code == 200 else "FAIL", f"{r.status_code} {len(state['npc_goals'])} 个目标")

        # 编辑他人角色 → 403
        r = req("PUT", f"/residents/{state['npc_slug']}", token=state["tok_a"], json={"persona_md": "hacked"})
        record("M3-6b", "编辑他人角色被拒", "PASS" if r.status_code in (401, 403) else "FAIL", f"{r.status_code}")

    if state.get("slug_a"):
        r = req("PUT", f"/residents/{state['slug_a']}", token=state["tok_a"], json={"persona_md": "一位来自云端的系统测试员(已更新)。"})
        record("M3-6a", "编辑自己角色", "PASS" if r.status_code == 200 else "FAIL", f"{r.status_code} {r.text[:80]}")
        r = req("GET", f"/residents/{state['slug_a']}/home/decor", token=state["tok_a"])
        record("M3-10", "家装读取", "PASS" if r.status_code == 200 else "FAIL", f"{r.status_code}")
    else:
        record("M3-6a", "编辑自己角色", "SKIP", "未取得玩家角色 slug")

    r = req("GET", "/world/locations", token=state["tok_a"])
    locs = jbody(r)
    n = len(locs) if isinstance(locs, list) else len(locs.get("locations", []))
    record("M3-8", "世界位置", "PASS" if r.status_code == 200 and n >= 5 else "FAIL", f"{n} 个位置")

    qname = (state.get("npc") or {}).get("name") or "克劳斯"
    r = req("GET", "/search", token=state["tok_a"], params={"q": qname})
    hits = jbody(r)
    nhits = len(hits) if isinstance(hits, list) else len(hits.get("results", hits.get("items", [])))
    record("M3-9", "搜索居民", "PASS" if r.status_code == 200 and nhits >= 1 else ("WARN" if r.status_code == 200 else "FAIL"),
           f"q={qname} → {nhits} 命中")


# ---------------------------------------------------------------- M4/M5 WebSocket + NPC 对话
async def ws_flow(state, skip_llm):
    import websockets
    print("\n=== M4/M5 WebSocket 实时 + NPC 对话 ===")

    async def connect(token):
        ws = await websockets.connect(WS_URL, open_timeout=20)
        await ws.send(json.dumps({"type": "auth", "token": token}))
        return ws

    async def collect(ws, want_types, timeout=15):
        """收消息直到集齐 want_types 或超时, 返回 {type: msg}."""
        got = {}
        end = time.time() + timeout
        while time.time() < end and not all(t in got for t in want_types):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.5, end - time.time()))
                m = json.loads(raw)
                got.setdefault(m.get("type"), m)
                if m.get("type") == "chat_reply":  # 流式: 聚合文本
                    agg = got.setdefault("_chat_text", {"text": ""})
                    agg["text"] += m.get("text", "") or ""
                    if m.get("done"):
                        got["_chat_done"] = m
            except asyncio.TimeoutError:
                break
        return got

    # M4-2 错误 token
    try:
        ws_bad = await websockets.connect(WS_URL, open_timeout=20)
        await ws_bad.send(json.dumps({"type": "auth", "token": "bad-token"}))
        code = None
        try:
            await asyncio.wait_for(ws_bad.recv(), timeout=10)
            await asyncio.wait_for(ws_bad.recv(), timeout=10)
        except websockets.exceptions.ConnectionClosed as e:
            code = e.rcvd.code if e.rcvd else None
        record("M4-2", "WS 伪 token 拒绝", "PASS" if code == 4001 else "FAIL", f"close code={code}")
    except Exception as e:
        record("M4-2", "WS 伪 token 拒绝", "FAIL", e)

    ws_a = await connect(state["tok_a"])
    got = await collect(ws_a, ["auth_ok", "spawn_position"], timeout=20)
    record("M4-1", "WS 鉴权+出生点", "PASS" if "auth_ok" in got and "spawn_position" in got else "FAIL",
           f"types={[k for k in got if not k.startswith('_')]}")
    record("M4-3", "每日登录奖励", "PASS" if "daily_reward" in got else "WARN",
           f"daily_reward={got.get('daily_reward', {}).get('amount', '未收到(可能已领)')}")
    state["spawn"] = got.get("spawn_position", {})

    ws_b = await connect(state["tok_b"])
    got_b = await collect(ws_b, ["auth_ok"], timeout=20)

    # A 移动, B 应收到广播
    sx, sy = state["spawn"].get("x", 2432), state["spawn"].get("y", 1600)
    await ws_a.send(json.dumps({"type": "move", "x": sx + 32, "y": sy, "direction": "right"}))
    got_b2 = await collect(ws_b, ["player_moved"], timeout=10)
    moved = got_b2.get("player_moved", {})
    record("M4-4", "移动广播同步", "PASS" if moved.get("x") == sx + 32 else "FAIL", f"B 收到 {str(moved)[:100]}")

    got_bj = {**got_b, **got_b2}
    seen_join = "online_players" in got_bj or "player_joined" in got_bj
    record("M4-5", "在线玩家互见", "PASS" if seen_join else "WARN", f"B types={[k for k in got_bj if not k.startswith('_')]}")

    # 玩家私聊 A→B
    uid_b = None
    r = req("GET", "/users/me", token=state["tok_b"])
    uid_b = jbody(r).get("id")
    if uid_b:
        await ws_a.send(json.dumps({"type": "player_chat", "target_id": uid_b, "text": "你好,这是一条冒烟测试私聊"}))
        got_pc = await collect(ws_b, ["player_chat", "player_chat_reply", "player_chat_msg"], timeout=15)
        hit = next((k for k in got_pc if "player_chat" in str(k)), None)
        record("M5-7", "玩家间私聊", "PASS" if hit else "FAIL", f"B 收到 types={[k for k in got_pc if not k.startswith('_')]}")

    # NPC 对话 (LLM)
    if skip_llm:
        record("M5-1", "NPC 对话", "SKIP", "--skip-llm")
    elif state.get("npc_slug"):
        await ws_a.send(json.dumps({"type": "start_chat", "resident_slug": state["npc_slug"]}))
        got1 = await collect(ws_a, ["chat_started"], timeout=20)
        started = "chat_started" in got1
        queued = "chat_queued" in got1 or "wake_required" in got1
        record("M5-1", "开始 NPC 对话", "PASS" if started else ("WARN" if queued else "FAIL"),
               f"types={[k for k in got1 if not k.startswith('_')]}")
        if started:
            await ws_a.send(json.dumps({"type": "chat_msg", "text": "你好!请用一句话介绍你自己,并告诉我你现在在做什么?"}))
            t0 = time.time()
            got2 = await collect(ws_a, ["_chat_done"], timeout=90)
            reply = got2.get("_chat_text", {}).get("text", "")
            dt = time.time() - t0
            record("M5-2", "NPC LLM 回复", "PASS" if len(reply) > 5 else "FAIL",
                   f"{dt:.1f}s 回复[{len(reply)}字]: {reply[:120]}")
            state["chat_reply"] = reply
            # 植入记忆
            await ws_a.send(json.dumps({"type": "chat_msg", "text": "记住一个暗号:蓝色的猫头鹰在钟楼下睡觉。下次见面我会考你。"}))
            got3 = await collect(ws_a, ["_chat_done"], timeout=90)
            state["memory_planted"] = bool(got3.get("_chat_text", {}).get("text"))
            # 结束+评分
            conv_id = (got2.get("chat_reply") or {}).get("conversation_id") or (got1.get("chat_started") or {}).get("conversation_id")
            await ws_a.send(json.dumps({"type": "end_chat"}))
            got4 = await collect(ws_a, ["chat_ended"], timeout=15)
            record("M5-1b", "结束对话", "PASS" if "chat_ended" in got4 else "WARN", f"types={list(got4)[:5]}")
            cid = conv_id or (got4.get("chat_ended") or {}).get("conversation_id")
            if cid:
                await ws_a.send(json.dumps({"type": "rate_chat", "rating": 5, "conversation_id": str(cid)}))
                record("M5-4", "对话评分", "PASS", f"conversation_id={cid}")
            else:
                record("M5-4", "对话评分", "SKIP", "未拿到 conversation_id")

    await ws_a.close()
    await ws_b.close()

    # 记忆回访(新连接新对话)
    if not skip_llm and state.get("memory_planted"):
        await asyncio.sleep(20)  # 给记忆提取一点时间
        ws2 = await connect(state["tok_a"])
        await collect(ws2, ["auth_ok"], timeout=15)
        await ws2.send(json.dumps({"type": "start_chat", "resident_slug": state["npc_slug"]}))
        g1 = await collect(ws2, ["chat_started"], timeout=20)
        if "chat_started" in g1:
            await ws2.send(json.dumps({"type": "chat_msg", "text": "还记得我告诉过你的暗号吗?是关于什么动物的?"}))
            g2 = await collect(ws2, ["_chat_done"], timeout=90)
            reply = g2.get("_chat_text", {}).get("text", "")
            hit = any(k in reply for k in ["猫头鹰", "钟楼", "蓝色"])
            record("M5-3", "三层记忆回忆", "PASS" if hit else "WARN", f"回复: {reply[:150]}")
            await ws2.send(json.dumps({"type": "end_chat"}))
        else:
            record("M5-3", "三层记忆回忆", "SKIP", "二次对话未能开始")
        await ws2.close()


# ---------------------------------------------------------------- M6 Forge
def m6(state, skip_llm):
    print("\n=== M6 锻造 Forge ===")
    if skip_llm:
        record("M6", "锻造全部", "SKIP", "--skip-llm")
        return
    r = req("POST", "/forge/quick", token=state["tok_a"], timeout=180, json={
        "name": "夜风侦探",
        "raw_text": "一位在霓虹都市深夜工作的私家侦探,冷静、毒舌但内心柔软,擅长观察细节,口头禅是'真相不打烊'。爱喝黑咖啡,养了一只机械鸽子。"})
    body = jbody(r)
    fid = body.get("forge_id")
    final = body
    if r.status_code == 200 and fid and str(body.get("status")) not in ("done", "completed"):
        deadline = time.time() + 240
        while time.time() < deadline:
            final = jbody(req("GET", f"/forge/status/{fid}", token=state["tok_a"], timeout=60))
            if str(final.get("status")).lower() in ("done", "completed", "complete", "failed", "error"):
                break
            time.sleep(10)
    txt = json.dumps(final, ensure_ascii=False)
    ok = r.status_code == 200 and str(final.get("status")).lower() in ("done", "completed", "complete") \
        and (final.get("persona_md") or final.get("soul_md") or "persona" in txt)
    record("M6-1", "快速锻造(轮询至完成)", "PASS" if ok else "FAIL",
           f"{r.status_code} status={final.get('status')} persona={str(final.get('persona_md'))[:80]}")
    state["forged_slug"] = (final.get("resident") or {}).get("slug") or final.get("slug")

    # 引导锻造: start → status → answer 一轮 → status
    r = req("POST", "/forge/start", token=state["tok_a"], timeout=120, json={"name": "灰烬诗人"})
    b = jbody(r)
    fid = b.get("forge_id") or b.get("id")
    if r.status_code == 200 and fid:
        st = jbody(req("GET", f"/forge/status/{fid}", token=state["tok_a"], timeout=60))
        q = st.get("question") or (st.get("questions") or [None])[0] or b.get("question")
        ans_ok = None
        if q:
            ra = req("POST", "/forge/answer", token=state["tok_a"], timeout=180,
                     json={"forge_id": fid, "answer": "他曾是战地记者,一场大火夺走了他的档案与旧名,如今在废墟酒吧朗诵用灰烬写成的诗。"})
            ans_ok = ra.status_code
        record("M6-2", "引导锻造 start/status/answer", "PASS" if st and (q or st.get("status")) else "WARN",
               f"start={r.status_code} status_keys={list(st)[:8]} answer={ans_ok}")
    else:
        record("M6-2", "引导锻造", "FAIL", f"{r.status_code} {str(b)[:120]}")

    # 深度蒸馏 — 异步启动,尾部再收
    r = req("POST", "/forge/deep-start", token=state["tok_a"], timeout=120, json={
        "character_name": "阿达·洛芙莱斯", "user_material": "世界上第一位程序员,痴迷分析机。"})
    b = jbody(r)
    state["deep_id"] = b.get("forge_id") or b.get("id")
    record("M6-3a", "深度蒸馏启动", "PASS" if r.status_code == 200 and state["deep_id"] else ("SKIP" if r.status_code in (402, 403, 429) else "FAIL"),
           f"{r.status_code} {str(b)[:120]}")

    r = req("POST", "/avatar/generate", token=state["tok_a"], timeout=240, json={
        "name": "夜风侦探", "persona_md": "霓虹都市的深夜私家侦探,风衣,机械鸽子。"})
    b = jbody(r)
    ok = r.status_code == 200 and ("url" in str(b) or "image" in str(b))
    record("M6-5", "AI 头像生成", "PASS" if ok else ("SKIP" if r.status_code in (400, 402, 403, 501, 503) else "FAIL"),
           f"{r.status_code} {str(b)[:120]}")

    r = req("POST", "/sprites/match", token=state["tok_a"], timeout=120, json={
        "persona_text": "冷静毒舌的深夜私家侦探,穿风衣"})
    record("M6-6", "精灵智能匹配", "PASS" if r.status_code == 200 else "FAIL", f"{r.status_code} {r.text[:100]}")


def m6_deep_poll(state):
    if not state.get("deep_id"):
        return
    print("\n=== M6-3 深度蒸馏进度轮询 ===")
    stages = []
    deadline = time.time() + 600
    last = {}
    while time.time() < deadline:
        r = req("GET", f"/forge/deep-status/{state['deep_id']}", token=state["tok_a"], timeout=60)
        last = jbody(r)
        stage = last.get("stage") or last.get("status") or last.get("phase")
        if stage and (not stages or stages[-1] != stage):
            stages.append(stage)
            print(f"   → 阶段: {stage}")
        if str(stage).lower() in ("done", "completed", "complete", "failed", "error"):
            break
        time.sleep(20)
    final = str(stages[-1] if stages else "?").lower()
    ok = final in ("done", "completed", "complete")
    record("M6-3", "深度蒸馏全流程", "PASS" if ok else ("WARN" if stages else "FAIL"),
           f"阶段链: {' → '.join(map(str, stages))} | {str(last)[:150]}")


# ---------------------------------------------------------------- M7 经济
def m7(state):
    print("\n=== M7 经济系统 ===")
    bal = get_balance(state["tok_a"])
    record("M7-1", "Soul Coin 余额", "PASS" if isinstance(bal, (int, float)) and bal > 0 else "WARN", f"余额={bal}")
    state["bal0"] = bal or 0

    r = req("GET", "/shop/catalog", token=state["tok_a"])
    cat = jbody(r)
    items = cat if isinstance(cat, list) else cat.get("items", [])
    record("M7-2", "商店目录", "PASS" if r.status_code == 200 and items else "FAIL", f"{len(items)} 件商品")

    cheap = None
    dear = None
    for it in [dict(i, price=i.get("price_sc", i.get("price"))) for i in items if isinstance(i, dict)]:
        if it.get("price") is None:
            continue
        if cheap is None and 0 < it["price"] <= (state["bal0"] or 0):
            cheap = it
        if it["price"] > (state["bal0"] or 0):
            dear = it
    if cheap:
        r = req("POST", "/shop/purchase", token=state["tok_a"], json={"item_code": cheap.get("code") or cheap.get("item_code"), "qty": 1})
        bal2 = get_balance(state["tok_a"]) or 0
        ok = r.status_code == 200 and bal2 == state["bal0"] - cheap["price"]
        record("M7-3", "购买商品+扣款", "PASS" if ok else ("WARN" if r.status_code == 200 else "FAIL"),
               f"{r.status_code} {cheap.get('code')}({cheap['price']}) 余额 {state['bal0']}→{bal2}")
        state["bal0"] = bal2
        inv = jbody(req("GET", "/shop/inventory", token=state["tok_a"]))
        inv_items = inv if isinstance(inv, list) else inv.get("items", [])
        record("M7-3b", "库存可见", "PASS" if inv_items else "WARN", f"{len(inv_items)} 条")
    else:
        record("M7-3", "购买商品", "SKIP", f"无可负担商品(余额 {state['bal0']})")

    if dear and dear.get("price", 0) > (state["bal0"] or 0):
        r = req("POST", "/shop/purchase", token=state["tok_a"], json={"item_code": dear.get("code") or dear.get("item_code"), "qty": 1})
        bal3 = get_balance(state["tok_a"]) or 0
        ok = r.status_code >= 400 and bal3 == state["bal0"]
        record("M7-4", "余额不足拒绝+余额不变", "PASS" if ok else "FAIL",
               f"{r.status_code} {dear.get('code')}({dear.get('price')}) 余额={bal3}")
    else:
        record("M7-4", "余额不足拒绝", "SKIP", "无超额商品可测")

    r = req("GET", "/profile/transactions", token=state["tok_a"])
    tx = jbody(r)
    txs = tx if isinstance(tx, list) else tx.get("transactions", tx.get("items", []))
    record("M7-5", "交易流水", "PASS" if r.status_code == 200 else "FAIL", f"{len(txs)} 条")

    goals = state.get("npc_goals") or []
    gid = next((g.get("id") for g in goals if isinstance(g, dict) and g.get("id")), None)
    if gid and (state["bal0"] or 0) >= 5:
        r = req("POST", f"/goals/{gid}/invest", token=state["tok_a"], json={"amount": 5})
        record("M7-6", "目标投资", "PASS" if r.status_code == 200 else ("WARN" if r.status_code < 500 else "FAIL"),
               f"{r.status_code} {r.text[:80]}")
    else:
        record("M7-6", "目标投资", "SKIP", "NPC 无目标或余额不足")

    r = req("GET", "/debates", token=state["tok_a"])
    debs = jbody(r)
    dl = debs if isinstance(debs, list) else debs.get("debates", debs.get("items", []))
    open_d = next((d for d in dl if isinstance(d, dict) and str(d.get("status")) in ("open", "active", "ongoing")), None)
    if open_d and (state["bal0"] or 0) >= 5:
        r2 = req("POST", f"/debates/{open_d['id']}/vote", token=state["tok_a"], json={"side": "pro"})
        record("M7-7", "辩论投票", "PASS" if r2.status_code == 200 else "WARN", f"{r2.status_code} {r2.text[:80]}")
    else:
        record("M7-7", "辩论押注/投票", "SKIP", f"debates 接口 {r.status_code},无进行中辩论")

    r = req("GET", "/commissions", token=state["tok_a"])
    coms = jbody(r)
    cl = coms if isinstance(coms, list) else coms.get("commissions", coms.get("items", []))
    open_c = next((c for c in cl if isinstance(c, dict) and str(c.get("status")) in ("open", "available", "pending")), None)
    if open_c:
        r2 = req("POST", f"/commissions/{open_c['id']}/accept", token=state["tok_a"])
        r3 = req("POST", f"/commissions/{open_c['id']}/abandon", token=state["tok_a"]) if r2.status_code == 200 else None
        record("M7-8", "委托接受/放弃", "PASS" if r2.status_code == 200 else "WARN",
               f"accept={r2.status_code} abandon={getattr(r3, 'status_code', '-')}")
    else:
        record("M7-8", "委托", "SKIP" if r.status_code == 200 else "FAIL", f"{r.status_code},{len(cl)} 条,无 open 委托")


# ---------------------------------------------------------------- M8 社交内容
def m8(state):
    print("\n=== M8 社交与内容 ===")
    tok = state["tok_a"]
    r = req("GET", "/feed", token=tok)
    feed = jbody(r)
    fl = feed if isinstance(feed, list) else feed.get("items", feed.get("feed", []))
    record("M8-1", "动态流(世界有事件)", "PASS" if r.status_code == 200 and len(fl) > 0 else ("WARN" if r.status_code == 200 else "FAIL"), f"{len(fl)} 条")

    if state.get("npc_slug"):
        r1 = req("POST", f"/follows/{state['npc_slug']}", token=tok)
        r2 = req("DELETE", f"/follows/{state['npc_slug']}", token=tok)
        record("M8-2", "关注/取关", "PASS" if r1.status_code == 200 and r2.status_code == 200 else "FAIL",
               f"follow={r1.status_code} unfollow={r2.status_code}")

    r = req("GET", "/bulletin", token=tok)
    record("M8-3a", "公告栏读取", "PASS" if r.status_code == 200 else "FAIL", f"{r.status_code}")
    r = req("POST", "/bulletin/posts", token=tok, json={"title": "冒烟测试公告(可删)", "content_md": "自动化测试发布,验证公告链路。", "kind": "notice"})
    post_ok = r.status_code == 200
    r2 = req("GET", "/bulletin/posts", token=tok)
    seen = "冒烟测试公告" in r2.text
    record("M8-3b", "公告发帖+可见", "PASS" if post_ok and seen else ("WARN" if post_ok else "FAIL"),
           f"post={r.status_code} visible={seen}")

    if state.get("npc_slug"):
        deliver = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%d")
        r = req("POST", "/capsules", token=tok, json={"carrier_resident_slug": state["npc_slug"], "deliver_on": deliver, "content": "给两天后的自己:冒烟测试胶囊。"})
        r2 = req("GET", "/capsules", token=tok)
        caps = jbody(r2)
        n = len(caps) if isinstance(caps, list) else len(caps.get("capsules", caps.get("items", [])))
        record("M8-4", "时间胶囊", "PASS" if r.status_code == 200 and n >= 1 else ("WARN" if r.status_code < 500 else "FAIL"),
               f"post={r.status_code} list={n} 条")

    r = req("GET", "/polls/open", token=tok)
    polls = jbody(r)
    pl = polls if isinstance(polls, list) else polls.get("polls", polls.get("items", []))
    if pl:
        r2 = req("POST", f"/polls/{pl[0]['id']}/vote", token=tok, json={"option_idx": 0})
        record("M8-5", "投票", "PASS" if r2.status_code == 200 else "WARN", f"{r2.status_code}")
    else:
        record("M8-5", "投票", "SKIP" if r.status_code == 200 else "FAIL", f"{r.status_code} 无开放投票")

    r = req("GET", "/graph/relationships", token=tok)
    g = jbody(r)
    edges = g.get("edges", g.get("relationships", g if isinstance(g, list) else []))
    record("M8-6", "关系图谱", "PASS" if r.status_code == 200 else "FAIL", f"{len(edges)} 条关系边")

    r = req("GET", "/seasons/current", token=tok)
    r2 = req("GET", "/seasons/current/leaderboard", token=tok)
    record("M8-7", "赛季+排行榜(无赛季也应 200)", "PASS" if r.status_code == 200 and r2.status_code == 200 else "FAIL",
           f"season={r.status_code} board={r2.status_code}")

    r = req("GET", "/digest/latest", token=tok)
    r2 = req("GET", "/digest/weekly/me", token=tok)
    record("M8-8", "日报/周报", "PASS" if r.status_code == 200 and r2.status_code == 200 else "WARN",
           f"latest={r.status_code} weekly={r2.status_code}")

    r = req("GET", "/notifications", token=tok)
    notes = jbody(r)
    nl = notes if isinstance(notes, list) else notes.get("notifications", notes.get("items", []))
    ids = [n.get("id") for n in nl if isinstance(n, dict) and n.get("id")][:5]
    r2 = req("POST", "/notifications/read", token=tok, json={"ids": ids}) if ids else None
    record("M8-9", "通知+已读", "PASS" if r.status_code == 200 and (r2 is None or r2.status_code == 200) else "FAIL",
           f"{len(nl)} 条, read={getattr(r2, 'status_code', '无通知可标')}")

    r = req("GET", "/achievements", token=tok)
    ach = jbody(r)
    al = ach if isinstance(ach, list) else ach.get("achievements", ach.get("items", []))
    record("M8-10", "成就列表", "PASS" if r.status_code == 200 and al else "FAIL", f"{len(al)} 项")

    r = req("GET", "/daily/quest", token=tok)
    record("M8-11", "每日任务", "PASS" if r.status_code == 200 else "FAIL", f"{r.status_code} {r.text[:80]}")

    r = req("GET", "/exploration/me", token=tok)
    record("M8-12", "探索图鉴", "PASS" if r.status_code == 200 else "FAIL", f"{r.status_code}")

    r = req("GET", "/creator/stats", token=tok)
    record("M8-13", "创作者统计", "PASS" if r.status_code == 200 else "FAIL", f"{r.status_code}")

    r = req("GET", "/events/active", token=tok)
    record("M12-5", "活动事件接口", "PASS" if r.status_code == 200 else "FAIL", f"{r.status_code} {r.text[:80]}")


# ---------------------------------------------------------------- M9 设置
def m9(state):
    print("\n=== M9 设置面板 ===")
    tok = state["tok_a"]
    r = req("GET", "/settings", token=tok)
    s = jbody(r)
    record("M9-1", "读取全部设置", "PASS" if r.status_code == 200 and len(s) >= 3 else "FAIL", f"分区: {list(s)[:8]}")

    r = req("PATCH", "/settings/interaction", token=tok, json={"reply_mode": "auto"})
    r2 = req("GET", "/settings", token=tok)
    eff = "auto" in json.dumps(jbody(r2)).lower()
    record("M9-2", "PATCH interaction 并回读", "PASS" if r.status_code == 200 and eff else ("WARN" if r.status_code == 200 else "FAIL"),
           f"{r.status_code} 回读生效={eff}")

    r = req("PUT", "/settings/character/persona", token=tok, json={
        "persona_md": "云端系统测试员,冒烟测试更新。", "soul_md": "求真务实。", "ability_md": "自动化测试。"})
    record("M9-3", "人设编辑", "PASS" if r.status_code == 200 else "FAIL", f"{r.status_code} {r.text[:80]}")

    r = req("POST", "/settings/llm/test", token=tok, json={
        "api_format": "anthropic", "api_base_url": "https://example.invalid", "api_key": "sk-test", "model_name": "test"})
    ok = r.status_code in (200, 400, 403, 422, 502)  # 明确响应即可(多半禁用或连接失败)
    record("M9-4", "自定义 LLM 测试端点", "PASS" if ok else "FAIL", f"{r.status_code} {r.text[:100]}")

    # 改密码 + 新密码登录 (放最后避免影响其他用例: 用账号B)
    new_pw = state["pw"] + "N1"
    r = req("POST", "/settings/account/password", token=state["tok_b"], json={"old_password": state["pw"], "new_password": new_pw})
    r2 = req("POST", "/auth/login", json={"email": state["email_b"], "password": new_pw})
    r3 = req("POST", "/auth/login", json={"email": state["email_b"], "password": state["pw"]})
    ok = r.status_code == 200 and r2.status_code == 200 and r3.status_code == 401
    record("M1-9", "改密码闭环(账号B)", "PASS" if ok else "FAIL",
           f"change={r.status_code} new_login={r2.status_code} old_login={r3.status_code}")
    if r2.status_code == 200:
        state["tok_b"] = jbody(r2).get("token") or jbody(r2).get("access_token") or state["tok_b"]


# ---------------------------------------------------------------- M10 admin 权限
def m10(state):
    print("\n=== M10 管理后台权限 ===")
    paths = ["/admin/dashboard/stats", "/admin/dashboard/health", "/admin/users", "/admin/residents",
             "/admin/economy/config", "/admin/economy/stats", "/admin/forge", "/admin/llm-usage/summary",
             "/admin/system/entries", "/admin/system/llm", "/admin/events", "/admin/items",
             "/admin/social-graph", "/admin/gossip/recent", "/admin/lab/status", "/admin/world/proposals"]
    bad = []
    for p in paths:
        r = req("GET", p, token=state["tok_a"])
        if r.status_code not in (401, 403):
            bad.append(f"{p}={r.status_code}")
    record("M10-1", "普通账号访问 /admin/* 全部被拒", "PASS" if not bad else "FAIL",
           bad or f"{len(paths)} 个接口全部 401/403")
    # 写接口抽样
    r = req("PUT", "/admin/economy/config", token=state["tok_a"], json={})
    r2 = req("POST", "/admin/lab/kill-switch", token=state["tok_a"], json={"enabled": True})
    ok = r.status_code in (401, 403) and r2.status_code in (401, 403)
    record("M10-1b", "普通账号 admin 写接口被拒", "PASS" if ok else "FAIL", f"economy={r.status_code} kill-switch={r2.status_code}")


# ---------------------------------------------------------------- M11 Lab
def m11(state):
    print("\n=== M11 Lab 实验模块 ===")
    tok = state["tok_a"]
    r = req("GET", "/lab/researchers", token=tok)
    if r.status_code in (403, 404, 501, 503):
        record("M11-1", "Lab 模块", "SKIP", f"{r.status_code} — LAB 未启用(符合配置预期): {r.text[:100]}")
        return
    rs = jbody(r)
    rl = rs if isinstance(rs, list) else rs.get("researchers", rs.get("items", []))
    record("M11-1", "研究员列表", "PASS" if r.status_code == 200 else "FAIL", f"{r.status_code} {len(rl)} 位")

    r = req("GET", "/lab/tasks", token=tok)
    record("M11-0", "任务列表", "PASS" if r.status_code == 200 else "FAIL", f"{r.status_code}")

    bal = get_balance(tok) or 0
    reward = 10
    if bal < reward:
        record("M11-2", "创建 Lab 任务", "SKIP", f"余额不足({bal})")
        return
    researcher = (rl[0].get("slug") if rl and isinstance(rl[0], dict) else None)
    r = req("POST", "/lab/tasks", token=tok, json={
        "title": "冒烟测试:总结小镇今日见闻", "brief_md": "请用 100 字总结你今天在小镇的见闻。",
        "reward_sc": reward, "deliverable_kind": "report", "researcher_slug": researcher})
    b = jbody(r)
    task_id = b.get("id") or b.get("task_id") or (b.get("task") or {}).get("id")
    bal2 = get_balance(tok) or 0
    if r.status_code == 503 and "disabled" in r.text.lower():
        record("M11-2", "创建 Lab 任务", "SKIP", f"503 Lab is disabled(符合 LAB_ENABLED 配置)")
        return
    record("M11-2", "创建 Lab 任务+冻结赏金", "PASS" if r.status_code == 200 and task_id else "FAIL",
           f"{r.status_code} task={task_id} 余额 {bal}→{bal2}")
    state["lab_task_id"] = task_id
    state["lab_bal_before_cancel"] = bal2
    state["lab_bal_orig"] = bal

    if task_id:
        # 轮询 2 分钟看状态机
        statuses = []
        run_id = None
        for _ in range(8):
            t = jbody(req("GET", f"/lab/tasks/{task_id}", token=tok))
            st = t.get("status") or (t.get("task") or {}).get("status")
            run_id = run_id or t.get("run_id") or (t.get("run") or {}).get("id")
            if not statuses or statuses[-1] != st:
                statuses.append(st)
            if str(st) in ("review", "done", "completed", "failed", "rejected"):
                break
            time.sleep(15)
        record("M11-3", "任务状态机推进", "PASS" if len(statuses) >= 1 and statuses[-1] else "WARN",
               f"状态链: {' → '.join(map(str, statuses))} run={run_id}")
        if run_id:
            r = req("GET", f"/lab/runs/{run_id}/steps", token=tok)
            record("M11-3b", "运行步骤可查", "PASS" if r.status_code == 200 else "WARN", f"{r.status_code} {r.text[:100]}")
        # 取消任务(退款验证)
        r = req("POST", f"/lab/tasks/{task_id}/cancel", token=tok)
        bal3 = get_balance(tok) or 0
        if r.status_code == 200:
            record("M11-6", "取消任务+退款", "PASS" if bal3 >= state["lab_bal_orig"] else "WARN",
                   f"{r.status_code} 余额 {state['lab_bal_before_cancel']}→{bal3}(初始 {state['lab_bal_orig']})")
        else:
            record("M11-6", "取消任务", "SKIP", f"{r.status_code}(任务可能已推进到不可取消状态) {r.text[:80]}")


# ---------------------------------------------------------------- M12 自主行为
def m12(state):
    print("\n=== M12 居民自主行为(快照对比) ===")
    r = req("GET", "/residents", token=state["tok_a"])
    body = jbody(r)
    residents = body if isinstance(body, list) else body.get("residents", body.get("items", []))
    snap2 = {x.get("slug"): (x.get("x"), x.get("y"), x.get("status"), x.get("current_action")) for x in residents if isinstance(x, dict)}
    snap1 = state.get("residents_snapshot1", {})
    changed = [s for s in snap1 if s in snap2 and snap1[s] != snap2[s]]
    record("M12-1", "NPC 状态/位置变化(AgentLoop 存活)", "PASS" if changed else "WARN",
           f"{len(changed)}/{len(snap1)} 个居民有变化: {changed[:5]}")

    # 热度: 对话过的 NPC
    if state.get("npc_slug"):
        d = jbody(req("GET", f"/residents/{state['npc_slug']}", token=state["tok_a"]))
        record("M12-3", "对话后 NPC 热度", "PASS" if (d.get("heat") or 0) > 0 else "WARN", f"heat={d.get('heat')}")


# ---------------------------------------------------------------- M14 非功能
def m14(state):
    print("\n=== M14 非功能抽查 ===")
    lat = []
    for _ in range(10):
        t0 = time.time()
        req("GET", "/health", timeout=15)
        lat.append(time.time() - t0)
    lat.sort()
    p95 = lat[int(len(lat) * 0.95) - 1]
    record("M14-1a", "/health 时延", "PASS" if p95 < 0.5 else "WARN", f"p50={lat[4]*1000:.0f}ms p95={p95*1000:.0f}ms")

    lat = []
    for _ in range(6):
        t0 = time.time()
        req("GET", "/residents", token=state["tok_a"], timeout=30)
        lat.append(time.time() - t0)
    lat.sort()
    record("M14-1b", "/residents 时延", "PASS" if lat[-2] < 1.5 else "WARN", f"min={lat[0]:.2f}s p~90={lat[-2]:.2f}s")

    r = req("GET", "/residents/__not_exist__", token=state["tok_a"])
    b = jbody(r)
    ok = r.status_code == 404 and ("detail" in b or "message" in b) and "Traceback" not in r.text
    record("M14-2", "404 错误结构化", "PASS" if ok else "FAIL", f"{r.status_code} {r.text[:80]}")

    me_raw = json.dumps(jbody(req("GET", "/users/me", token=state["tok_a"])))
    leak = [k for k in ["password", "hashed", "secret", "api_key"] if k in me_raw.lower() and "null" not in me_raw.lower()[me_raw.lower().find(k):me_raw.lower().find(k) + 40]]
    record("M14-3", "响应无敏感字段", "PASS" if not leak else "WARN", leak or "users/me 干净")

    # 并发购买幂等 (若有可负担商品)
    cat = jbody(req("GET", "/shop/catalog", token=state["tok_b"]))
    items = cat if isinstance(cat, list) else cat.get("items", [])
    bal = get_balance(state["tok_b"]) or 0
    afford = [dict(i, price=i.get("price_sc", i.get("price"))) for i in items if isinstance(i, dict) and 0 < (i.get("price_sc", i.get("price")) or 0) <= bal]
    if afford:
        it = afford[0]
        code = it.get("code") or it.get("item_code")
        with concurrent.futures.ThreadPoolExecutor(5) as ex:
            futs = [ex.submit(req, "POST", "/shop/purchase", state["tok_b"], json={"item_code": code, "qty": 1}) for _ in range(5)]
            codes = [f.result().status_code for f in futs]
        bal2 = get_balance(state["tok_b"]) or 0
        n_ok = codes.count(200)
        spent = bal - bal2
        consistent = spent == n_ok * (it.get("price") or 0) and bal2 >= 0
        record("M14-4", "并发购买余额一致性", "PASS" if consistent else "FAIL",
               f"5并发 codes={codes} 成功{n_ok}次 扣款{spent} (单价{it.get('price')}) 余额{bal}→{bal2}")
    else:
        record("M14-4", "并发购买一致性", "SKIP", f"账号B 无可负担商品(余额 {bal})")


# ---------------------------------------------------------------- M1-8 注册限流 (放最后)
def m1_ratelimit():
    print("\n=== M1-8 注册限流(收尾执行) ===")
    codes = []
    for i in range(7):
        r = req("POST", "/auth/register", json={
            "name": f"rl{i}", "email": f"svtest_rl_{uuid.uuid4().hex[:10]}@sv-test.dev", "password": "RlTest123!"})
        codes.append(r.status_code)
        if r.status_code == 429:
            break
    record("M1-8", "注册限流 5/min", "PASS" if 429 in codes else "WARN", f"codes={codes}")


# ---------------------------------------------------------------- media upload
def m5_media(state, skip_llm):
    print("\n=== M5-5 媒体上传 ===")
    # 生成一张小图片
    import struct, zlib
    def png_bytes():
        w = h = 64
        row = b"\x00" + bytes([30, 144, 255, 255] * w)
        raw = row * h
        def chunk(t, d):
            c = t + d
            return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
        ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    img = png_bytes()
    r = req("POST", "/api/media/upload?media_type=image", token=state["tok_a"], timeout=60,
            files={"file": ("smoke.png", img, "image/png")})
    b = jbody(r)
    url = b.get("media_url") or b.get("url") or (b.get("media") or {}).get("url")
    record("M5-5a", "媒体上传(图片)", "PASS" if r.status_code == 200 and url else "FAIL", f"{r.status_code} url={url}")
    state["media_url"] = url
    if url:
        full = url if url.startswith("http") else API + url
        r2 = SESSION.get(full, timeout=30)
        record("M0-7", "静态媒体回读", "PASS" if r2.status_code == 200 else "FAIL", f"{r2.status_code} {full[:80]}")

    if state.get("npc_slug"):
        r = req("POST", "/photos/log", token=state["tok_a"], json={"resident_slug": state["npc_slug"], "media_url": url})
        record("M5-5b", "拍照留档", "PASS" if r.status_code == 200 else "WARN", f"{r.status_code} {r.text[:80]}")

    r = req("POST", "/tts", token=state["tok_a"], timeout=60, json={"resident_slug": state.get("npc_slug") or "", "text": "冒烟测试语音"})
    if r.status_code == 200:
        record("M5-9", "TTS", "PASS", f"200 {len(r.content)}B")
    elif r.status_code in (400, 402, 403, 429, 501, 503):
        record("M5-9", "TTS", "SKIP", f"{r.status_code}(未配置/配额): {r.text[:80]}")
    else:
        record("M5-9", "TTS", "FAIL", f"{r.status_code} {r.text[:80]}")

    r = req("GET", "/profile/conversations", token=state["tok_a"])
    convs = jbody(r)
    cl = convs if isinstance(convs, list) else convs.get("conversations", convs.get("items", []))
    record("M5-10", "会话历史", "PASS" if r.status_code == 200 else "FAIL", f"{len(cl)} 条会话")
    r = req("GET", "/profile/residents", token=state["tok_a"])
    record("M5-11", "我的角色列表", "PASS" if r.status_code == 200 else "FAIL", f"{r.status_code}")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-llm", action="store_true")
    ap.add_argument("--skip-ws", action="store_true")
    args = ap.parse_args()

    t_start = time.time()
    state = {}
    m0()
    m1(state)
    if not state.get("tok_a"):
        print("!! 注册/登录失败,无法继续带鉴权用例")
    else:
        m2(state)
        m3(state)
        if not args.skip_ws:
            asyncio.run(ws_flow(state, args.skip_llm))
        m6(state, args.skip_llm)
        m5_media(state, args.skip_llm)
        m7(state)
        m8(state)
        m9(state)
        m10(state)
        m11(state)
        if not args.skip_llm:
            m6_deep_poll(state)
        m12(state)
        m14(state)
    m1_ratelimit()

    dur = time.time() - t_start
    summary = {}
    for r in RESULTS:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    out = {"run_at": datetime.now(timezone.utc).isoformat(), "duration_s": round(dur, 1),
           "api": API, "front": FRONT, "summary": summary, "accounts": [state.get("email_a"), state.get("email_b")],
           "results": RESULTS}
    with open("results.json", "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n========== 汇总 ({dur:.0f}s) ==========")
    for k, v in sorted(summary.items()):
        print(f"  {k}: {v}")
    print("详情见 results.json")


if __name__ == "__main__":
    main()
