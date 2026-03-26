"""
YUA Agent — E2B Sandbox Proxy Service (Deploy on Render)

This service acts as a bridge between your platform (anything.com) and E2B cloud sandboxes.
Your AI agent calls this proxy via HTTP, and this proxy manages E2B sandboxes + forwards tool calls.

Architecture:
    anything.com (your platform) → This Render Proxy → E2B Cloud Sandboxes

All requests require X-Signature header for authentication.

Warm Pool:
    On startup the proxy pre-bootstraps WARM_POOL_SIZE sandboxes so they are instantly
    available when a user connects.  The agent calls POST /sandbox/acquire to get one
    immediately and POST /sandbox/release when the session ends.

Deploy on Render as a Docker Web Service.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ──────────────────── Configuration ────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("e2b-proxy")

SIGNATURE = os.environ.get("PROXY_SIGNATURE", "O3I3Eo1ZphHkfZolPCg81HXvVJVr5m7h")
E2B_API_KEY = os.environ.get("E2B_API_KEY", "")
E2B_TEMPLATE = os.environ.get("E2B_TEMPLATE", "xv9t5sqco7pw40w8flyb")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
RORK_IMAGE_URL = os.environ.get("RORK_IMAGE_URL", "https://toolkit.rork.com/images/generate/")
RORK_IMAGE_EDIT_URL = os.environ.get("RORK_IMAGE_EDIT_URL", "https://toolkit.rork.com/images/edit/")

SANDBOX_RUNTIME_PORT = 8330

# Warm pool config (set in Render dashboard or render.yaml)
_POOL_SIZE = int(os.environ.get("WARM_POOL_SIZE", "3"))
_POOL_REUSE_MODE = os.environ.get("WARM_POOL_REUSE_MODE", "reset")  # "reset" or "discard"
_SANDBOX_TIMEOUT = 600  # seconds; keepalive renews before expiry
_KEEPALIVE_INTERVAL = 60  # seconds between timeout renewals

# ──────────────────── App Setup ────────────────────


@dataclass
class PooledSandbox:
    sandbox_id: str           # our internal ID (also key in _sandboxes)
    url: str                  # https://... sandbox runtime URL
    vnc_url: str              # noVNC public URL (empty if not available)
    e2b_sandbox: Any          # e2b AsyncSandbox object (for set_timeout / kill)


# Active sandboxes (both assigned and returned by /sandbox/create legacy endpoint)
_sandboxes: dict[str, dict[str, Any]] = {}

# Warm pool state
_pool: asyncio.Queue[PooledSandbox] = asyncio.Queue()
_pool_assigned: dict[str, PooledSandbox] = {}   # sandbox_id → PooledSandbox
_pool_available: list[PooledSandbox] = []        # mirrors queue for keepalive
_pool_in_flight: int = 0                         # bootstraps currently in progress


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start warm pool on app boot, stop on shutdown."""
    if E2B_API_KEY:
        asyncio.create_task(_pool_start())
        asyncio.create_task(_pool_keepalive_loop())
        asyncio.create_task(_pool_refill_loop())
        logger.info("Warm pool initializing (%d sandboxes, reuse_mode=%s)...", _POOL_SIZE, _POOL_REUSE_MODE)
    else:
        logger.warning("E2B_API_KEY not set — warm pool disabled")

    yield

    # Shutdown: kill all pooled sandboxes
    logger.info("Shutting down warm pool...")
    for sb in list(_pool_available):
        await _kill_sandbox(sb)
    for sb in list(_pool_assigned.values()):
        await _kill_sandbox(sb)
    # Also destroy legacy sandboxes
    for info in list(_sandboxes.values()):
        esb = info.get("e2b_sandbox")
        if esb:
            try:
                await esb.kill()
            except Exception:
                pass


app = FastAPI(
    title="YUA E2B Proxy",
    description="E2B sandbox proxy with warm pool for YUA Agent",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────── Auth Middleware ────────────────────

def verify_signature(request: Request):
    sig = request.headers.get("X-Signature", "")
    if sig != SIGNATURE:
        raise HTTPException(status_code=401, detail="Invalid signature")


# ──────────────────── Helper: Forward to Sandbox ────────────────────

async def forward_to_sandbox(sandbox_id: str, path: str, body: dict, timeout: float = 120) -> dict:
    """Forward a request to the sandbox runtime API."""
    info = _sandboxes.get(sandbox_id)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Sandbox '{sandbox_id}' not found.")
    url = info["url"]
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(f"{url}{path}", json=body)
            resp.raise_for_status()
            return resp.json()
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail=f"Sandbox request timed out: {path}")
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=e.response.text[:500])
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Sandbox error: {e}")


# ──────────────────── Bootstrap E2B Sandbox ────────────────────

SANDBOX_CODE_DIR = Path(__file__).parent / "sandbox_runtime"


async def _run(sandbox, cmd: str, timeout: int = 15) -> Any:
    """Run a short-lived command with an explicit timeout. Never blocks > timeout seconds."""
    return await sandbox.commands.run(cmd, timeout=timeout)


async def bootstrap_e2b(sandbox) -> tuple[str, str]:
    """Bootstrap sandbox runtime into an E2B sandbox.

    Returns (runtime_url, vnc_url).

    All slow installs (pip, apt-get, wget) run as a nohup background script so
    no SDK command ever blocks for more than ~15 seconds, completely avoiding
    'context deadline exceeded' from E2B's HTTP streaming connection limits.
    """
    YUA_DIR = "/home/user/yua"
    cdp_port = "9222"

    # ── 1. Directory structure ────────────────────────────────────────────────
    await _run(sandbox,
        f"mkdir -p {YUA_DIR}/sandbox/browser/js {YUA_DIR}/sandbox/handlers {YUA_DIR}/sandbox/shell"
    )

    # ── 2. Upload sandbox runtime files ──────────────────────────────────────
    if SANDBOX_CODE_DIR.exists():
        logger.info("Uploading sandbox runtime to %s...", sandbox.sandbox_id)
        for local_path in SANDBOX_CODE_DIR.rglob("*"):
            if local_path.is_file() and not local_path.name.startswith("."):
                rel = local_path.relative_to(SANDBOX_CODE_DIR)
                remote_path = f"{YUA_DIR}/sandbox/{rel}"
                content = local_path.read_bytes()
                await sandbox.files.write(remote_path, content)
    else:
        logger.warning("No sandbox_runtime/ directory — using template's built-in runtime")

    # ── 3. Write install script to sandbox ──────────────────────────────────
    # The script uses 'trap EXIT' so it always writes the exit code to
    # /tmp/install.done, even on failure.  We then poll with short commands.
    install_script = b"""#!/bin/bash
trap 'echo $? > /tmp/install.done' EXIT
set -e

# ── Python deps ──────────────────────────────────────────────────────────────
if ! python3 -c 'import fastapi,uvicorn,httpx,websockets,aiofiles,pydantic' 2>/dev/null; then
    pip3 install --quiet fastapi 'uvicorn[standard]' httpx websockets aiofiles \
        pydantic pydantic-settings beautifulsoup4 pillow
fi

# ── Desktop tools (Xvfb, x11vnc, noVNC) ─────────────────────────────────────
if ! which Xvfb >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq xvfb x11vnc novnc websockify
fi

# ── Chrome browser ────────────────────────────────────────────────────────────
if ! which google-chrome-stable >/dev/null 2>&1 && ! which google-chrome >/dev/null 2>&1; then
    wget -q -O /tmp/chrome.deb \
        https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    apt-get install -y -qq /tmp/chrome.deb
    rm -f /tmp/chrome.deb
fi
"""
    await sandbox.files.write("/tmp/install.sh", install_script)

    # ── 4. Fire install script in background (returns in < 1s) ──────────────
    logger.info("Starting background install in %s...", sandbox.sandbox_id)
    await _run(sandbox,
        "chmod +x /tmp/install.sh && rm -f /tmp/install.done && "
        "nohup bash /tmp/install.sh > /tmp/install.log 2>&1 &"
    )

    # ── 5. Poll for install completion (each poll is a < 1s command) ─────────
    logger.info("Waiting for installs in %s (up to 10 min)...", sandbox.sandbox_id)
    for attempt in range(120):   # 120 × 5 s = 10 minutes max
        await asyncio.sleep(5)
        r = await _run(sandbox, "cat /tmp/install.done 2>/dev/null || echo pending")
        status = r.stdout.strip()
        if status == "0":
            logger.info(
                "Installs done in %s after ~%ds", sandbox.sandbox_id, (attempt + 1) * 5
            )
            break
        if status not in ("pending", ""):
            r2 = await _run(sandbox, "tail -40 /tmp/install.log 2>/dev/null")
            raise RuntimeError(
                f"Install failed (exit {status}) in {sandbox.sandbox_id}:\n{r2.stdout}"
            )
    else:
        r2 = await _run(sandbox, "tail -40 /tmp/install.log 2>/dev/null")
        raise RuntimeError(
            f"Install timed out after 10 min in {sandbox.sandbox_id}:\n{r2.stdout}"
        )

    # ── 6. Start Xvfb ────────────────────────────────────────────────────────
    await _run(sandbox,
        "Xvfb :0 -ac -screen 0 1280x960x24 -nolisten tcp > /tmp/xvfb.log 2>&1 &"
    )
    await asyncio.sleep(2)

    # ── 7. Start Chrome with CDP ──────────────────────────────────────────────
    r = await _run(sandbox, "which google-chrome-stable 2>/dev/null || echo google-chrome")
    chrome_bin = r.stdout.strip() or "google-chrome-stable"

    await _run(sandbox,
        f"export DISPLAY=:0 && {chrome_bin} "
        "--no-sandbox --disable-gpu --no-first-run --no-default-browser-check "
        "--password-store=basic --remote-debugging-port=9222 "
        "--remote-debugging-address=0.0.0.0 --user-data-dir=/tmp/chrome-data "
        "--force-device-scale-factor=0.8 --window-size=1280,960 "
        "--hide-crash-restore-bubble about:blank "
        "> /tmp/chrome.log 2>&1 &"
    )
    await asyncio.sleep(5)

    # ── 8. Start VNC (x11vnc + websockify/noVNC) ──────────────────────────────
    await _run(sandbox,
        "x11vnc -display :0 -nopw -listen 0.0.0.0 -rfbport 5900 -forever -shared -bg "
        "> /tmp/vnc.log 2>&1 && "
        "websockify --web /usr/share/novnc 6080 localhost:5900 > /tmp/novnc.log 2>&1 &"
    )
    await asyncio.sleep(2)

    # ── 9. Verify Chrome CDP ──────────────────────────────────────────────────
    for attempt in range(15):
        r = await _run(sandbox,
            f"curl -s http://localhost:{cdp_port}/json/version >/dev/null 2>&1 && echo OK || echo WAIT"
        )
        if "OK" in r.stdout:
            logger.info("Chrome CDP ready in sandbox %s", sandbox.sandbox_id)
            break
        await asyncio.sleep(2)

    # ── 10. Get noVNC public URL ──────────────────────────────────────────────
    try:
        vnc_url = f"https://{sandbox.get_host(6080)}/vnc.html"
        logger.info("VNC URL for %s: %s", sandbox.sandbox_id, vnc_url)
    except Exception:
        vnc_url = ""

    # ── 11. Start sandbox runtime ─────────────────────────────────────────────
    logger.info("Starting sandbox runtime in %s...", sandbox.sandbox_id)
    await _run(sandbox,
        f"cd {YUA_DIR} && CDP_PORT={cdp_port} PYTHONPATH={YUA_DIR} "
        f"nohup python3 -m uvicorn sandbox.runtime:app "
        f"--host 0.0.0.0 --port {SANDBOX_RUNTIME_PORT} "
        f"> /tmp/sandbox-runtime.log 2>&1 &"
    )

    # ── 12. Health check ──────────────────────────────────────────────────────
    logger.info("Waiting for runtime health in %s...", sandbox.sandbox_id)
    for _ in range(30):
        await asyncio.sleep(2)
        r = await _run(sandbox, f"curl -s http://localhost:{SANDBOX_RUNTIME_PORT}/healthz")
        if '"status":"ok"' in r.stdout:
            logger.info("Sandbox %s runtime healthy!", sandbox.sandbox_id)
            runtime_url = f"https://{sandbox.get_host(SANDBOX_RUNTIME_PORT)}"
            return runtime_url, vnc_url

    r = await _run(sandbox, "tail -30 /tmp/sandbox-runtime.log 2>/dev/null")
    logger.error("Runtime log for %s: %s", sandbox.sandbox_id, r.stdout)
    raise RuntimeError(f"Sandbox {sandbox.sandbox_id} runtime failed to start")


# ──────────────────── Warm Pool Internals ────────────────────

async def _pool_start() -> None:
    """Bootstrap WARM_POOL_SIZE sandboxes concurrently on startup."""
    logger.info("Pre-warming %d sandboxes...", _POOL_SIZE)
    tasks = [_pool_bootstrap_one() for _ in range(_POOL_SIZE)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ready = sum(1 for r in results if not isinstance(r, Exception))
    for r in results:
        if isinstance(r, Exception):
            logger.error("Failed to pre-warm sandbox: %s", r)
    logger.info("Warm pool ready: %d/%d sandboxes available", ready, _POOL_SIZE)


async def _pool_bootstrap_one() -> PooledSandbox:
    """Create one E2B sandbox, bootstrap it, add to pool."""
    global _pool_in_flight
    from e2b import AsyncSandbox

    _pool_in_flight += 1
    try:
        sandbox = await AsyncSandbox.create(
            template=E2B_TEMPLATE,
            api_key=E2B_API_KEY,
            timeout=_SANDBOX_TIMEOUT,
        )
        try:
            runtime_url, vnc_url = await bootstrap_e2b(sandbox)
        except Exception as e:
            logger.error("Bootstrap failed for %s: %s", sandbox.sandbox_id, e)
            try:
                await sandbox.kill()
            except Exception:
                pass
            raise
    finally:
        _pool_in_flight -= 1

    sb = PooledSandbox(
        sandbox_id=sandbox.sandbox_id,
        url=runtime_url,
        vnc_url=vnc_url,
        e2b_sandbox=sandbox,
    )
    # Register in _sandboxes so forward_to_sandbox() can reach it
    _sandboxes[sandbox.sandbox_id] = {"url": runtime_url, "e2b_sandbox": sandbox}
    _pool_available.append(sb)
    await _pool.put(sb)
    logger.info("Sandbox %s added to pool (available: %d)", sandbox.sandbox_id, _pool.qsize())
    return sb


async def _pool_keepalive_loop() -> None:
    """Refresh timeout on all pooled sandboxes so they don't expire."""
    while True:
        try:
            await asyncio.sleep(_KEEPALIVE_INTERVAL)
            for sb in list(_pool_available):
                try:
                    await sb.e2b_sandbox.set_timeout(_SANDBOX_TIMEOUT)
                except Exception as e:
                    logger.warning("Keepalive failed for %s: %s", sb.sandbox_id, e)
        except Exception as e:
            logger.error("Keepalive loop error: %s", e)


async def _pool_refill_loop() -> None:
    """Keep pool topped up after sandboxes are assigned or discarded."""
    while True:
        try:
            await asyncio.sleep(5)
            available = _pool.qsize()
            assigned = len(_pool_assigned)
            in_flight = _pool_in_flight
            total = available + assigned + in_flight
            if total < _POOL_SIZE:
                deficit = _POOL_SIZE - total
                logger.info(
                    "Pool below target (%d/%d, %d in-flight), bootstrapping %d replacement(s)...",
                    available + assigned, _POOL_SIZE, in_flight, deficit,
                )
                for _ in range(deficit):
                    asyncio.create_task(_pool_bootstrap_one_safe())
        except Exception as e:
            logger.error("Refill loop error: %s", e)


async def _pool_bootstrap_one_safe() -> None:
    """Fire-and-forget wrapper that logs errors."""
    try:
        await _pool_bootstrap_one()
    except Exception as e:
        logger.error("Failed to bootstrap replacement: %s", e)


async def _kill_sandbox(sb: PooledSandbox) -> None:
    _sandboxes.pop(sb.sandbox_id, None)
    if sb in _pool_available:
        _pool_available.remove(sb)
    try:
        await sb.e2b_sandbox.kill()
        logger.info("Killed sandbox %s", sb.sandbox_id)
    except Exception as e:
        logger.warning("Kill failed for %s: %s", sb.sandbox_id, e)


async def _reset_sandbox(sb: PooledSandbox) -> None:
    """Call /reset on the sandbox runtime to clear state for reuse."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{sb.url}/reset")
        if resp.status_code != 200:
            raise RuntimeError(f"Reset returned {resp.status_code}")
    # Wait for Chrome to restart, then confirm healthy
    await asyncio.sleep(3)
    for _ in range(20):
        await asyncio.sleep(2)
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"{sb.url}/healthz")
                if resp.status_code == 200:
                    await sb.e2b_sandbox.set_timeout(_SANDBOX_TIMEOUT)
                    return
        except Exception:
            pass
    raise RuntimeError(f"Sandbox {sb.sandbox_id} not healthy after reset")


# ══════════════════════════════════════════════════════════════
#                        ENDPOINTS
# ══════════════════════════════════════════════════════════════


# ──────────────────── Health Check ────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "yua-e2b-proxy",
        "active_sandboxes": len(_sandboxes),
        "pool_available": _pool.qsize(),
        "pool_assigned": len(_pool_assigned),
        "pool_target": _POOL_SIZE,
    }


# ──────────────────── Warm Pool: Acquire / Release ────────────────────

class AcquireRequest(BaseModel):
    timeout: int = 300  # seconds to wait for a sandbox to become available


class ReleaseRequest(BaseModel):
    sandbox_id: str
    reuse_mode: str = ""  # override WARM_POOL_REUSE_MODE; empty = use server default


@app.post("/sandbox/acquire")
async def sandbox_acquire(req: AcquireRequest, request: Request):
    """Pop a pre-warmed sandbox from the pool and assign it to the caller.

    Returns immediately if a sandbox is available, otherwise waits up to req.timeout.
    Response: {sandbox_id, url, vnc_url, status: "ready"}
    """
    verify_signature(request)

    available = _pool.qsize()
    logger.info("Sandbox acquire requested (pool available: %d)", available)

    try:
        sb = await asyncio.wait_for(_pool.get(), timeout=req.timeout)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503,
            detail=f"No warm sandbox available after {req.timeout}s — pool may be exhausted",
        )

    if sb in _pool_available:
        _pool_available.remove(sb)
    _pool_assigned[sb.sandbox_id] = sb
    logger.info("Sandbox %s acquired from pool", sb.sandbox_id)

    return {
        "sandbox_id": sb.sandbox_id,
        "url": sb.url,
        "vnc_url": sb.vnc_url,
        "status": "ready",
    }


@app.post("/sandbox/release")
async def sandbox_release(req: ReleaseRequest, request: Request):
    """Return a sandbox to the pool (reset) or destroy it (discard).

    After release the refill loop will ensure the pool stays at target size.
    """
    verify_signature(request)

    sb = _pool_assigned.pop(req.sandbox_id, None)
    if sb is None:
        # Also check legacy _sandboxes
        info = _sandboxes.pop(req.sandbox_id, None)
        if info is None:
            raise HTTPException(status_code=404, detail=f"Sandbox '{req.sandbox_id}' not found")
        esb = info.get("e2b_sandbox")
        if esb:
            try:
                await esb.kill()
            except Exception:
                pass
        return {"status": "released", "sandbox_id": req.sandbox_id}

    mode = req.reuse_mode or _POOL_REUSE_MODE
    if mode == "reset":
        try:
            logger.info("Resetting sandbox %s for pool reuse...", sb.sandbox_id)
            await _reset_sandbox(sb)
            _pool_available.append(sb)
            await _pool.put(sb)
            logger.info("Sandbox %s returned to pool", sb.sandbox_id)
            return {"status": "returned_to_pool", "sandbox_id": sb.sandbox_id}
        except Exception as e:
            logger.warning("Reset failed for %s (%s), discarding", sb.sandbox_id, e)

    # Discard path: kill and let refill loop create a replacement
    asyncio.create_task(_kill_sandbox(sb))
    return {"status": "discarded", "sandbox_id": sb.sandbox_id}


# ──────────────────── Legacy Sandbox Lifecycle ────────────────────

class SandboxCreateRequest(BaseModel):
    template: str | None = None
    timeout: int = 300


class SandboxDestroyRequest(BaseModel):
    sandbox_id: str


class SandboxStatusRequest(BaseModel):
    sandbox_id: str


@app.post("/sandbox/create")
async def sandbox_create(req: SandboxCreateRequest, request: Request):
    """Create a new E2B sandbox (legacy — prefer /sandbox/acquire for instant startup)."""
    verify_signature(request)

    if not E2B_API_KEY:
        raise HTTPException(status_code=500, detail="E2B_API_KEY not configured on proxy")

    from e2b import AsyncSandbox

    sandbox_id = uuid.uuid4().hex[:12]
    tmpl = req.template or E2B_TEMPLATE

    logger.info("Creating E2B sandbox (id=%s, template=%s)...", sandbox_id, tmpl)
    try:
        sandbox = await AsyncSandbox.create(template=tmpl, api_key=E2B_API_KEY, timeout=req.timeout)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"E2B sandbox creation failed: {e}")

    try:
        runtime_url, vnc_url = await bootstrap_e2b(sandbox)
    except Exception as e:
        logger.error("Bootstrap failed: %s", e)
        await sandbox.kill()
        raise HTTPException(status_code=502, detail=f"Sandbox bootstrap failed: {e}")

    _sandboxes[sandbox_id] = {"url": runtime_url, "e2b_sandbox": sandbox, "e2b_id": sandbox.sandbox_id}
    logger.info("Sandbox ready: %s → %s", sandbox_id, runtime_url)

    return {
        "sandbox_id": sandbox_id,
        "url": runtime_url,
        "vnc_url": vnc_url,
        "e2b_id": sandbox.sandbox_id,
        "status": "ready",
    }


@app.post("/sandbox/destroy")
async def sandbox_destroy(req: SandboxDestroyRequest, request: Request):
    verify_signature(request)
    info = _sandboxes.pop(req.sandbox_id, None)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Sandbox '{req.sandbox_id}' not found")
    esb = info.get("e2b_sandbox")
    if esb:
        try:
            await esb.kill()
        except Exception as e:
            logger.error("E2B destroy error: %s", e)
    return {"status": "destroyed", "sandbox_id": req.sandbox_id}


@app.post("/sandbox/status")
async def sandbox_status(req: SandboxStatusRequest, request: Request):
    verify_signature(request)
    info = _sandboxes.get(req.sandbox_id)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Sandbox '{req.sandbox_id}' not found")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{info['url']}/healthz")
            resp.raise_for_status()
            return {"status": "healthy", "sandbox_id": req.sandbox_id, "details": resp.json()}
    except Exception as e:
        return {"status": "unhealthy", "sandbox_id": req.sandbox_id, "error": str(e)}


@app.post("/sandbox/list")
async def sandbox_list(request: Request):
    verify_signature(request)
    return {
        "sandboxes": [
            {"sandbox_id": sid, "url": info["url"], "e2b_id": info.get("e2b_id", "")}
            for sid, info in _sandboxes.items()
        ],
        "pool_available": _pool.qsize(),
        "pool_assigned": len(_pool_assigned),
    }


# ──────────────────── Tool: Shell ────────────────────

class ShellRequest(BaseModel):
    sandbox_id: str
    action: str = "exec"
    command: str = ""
    session_id: str = "default"
    timeout: int = 120
    input: str = ""


@app.post("/tool/shell")
async def tool_shell(req: ShellRequest, request: Request):
    verify_signature(request)
    body: dict[str, Any] = {"session_id": req.session_id}
    if req.action == "exec":
        body["command"] = req.command
        body["timeout"] = req.timeout
        result = await forward_to_sandbox(req.sandbox_id, "/shell/exec", body, timeout=req.timeout + 5)
    elif req.action == "view":
        result = await forward_to_sandbox(req.sandbox_id, "/shell/view", body)
    elif req.action == "wait":
        body["timeout"] = req.timeout
        result = await forward_to_sandbox(req.sandbox_id, "/shell/wait", body, timeout=req.timeout + 5)
    elif req.action == "send":
        body["input"] = req.input
        result = await forward_to_sandbox(req.sandbox_id, "/shell/send", body)
    elif req.action == "kill":
        result = await forward_to_sandbox(req.sandbox_id, "/shell/kill", body)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown shell action: {req.action}")
    return result


# ──────────────────── Tool: File ────────────────────

class FileRequest(BaseModel):
    sandbox_id: str
    action: str = "read"
    path: str = ""
    content: str = ""
    old_text: str = ""
    new_text: str = ""
    offset: int = 0
    limit: int = 0
    encoding: str = ""


@app.post("/tool/file")
async def tool_file(req: FileRequest, request: Request):
    verify_signature(request)
    body: dict[str, Any] = {"path": req.path}
    if req.action == "read":
        if req.offset: body["offset"] = req.offset
        if req.limit: body["limit"] = req.limit
        result = await forward_to_sandbox(req.sandbox_id, "/file/read", body)
    elif req.action == "write":
        body["content"] = req.content
        if req.encoding: body["encoding"] = req.encoding
        result = await forward_to_sandbox(req.sandbox_id, "/file/write", body)
    elif req.action == "edit":
        body["old_text"] = req.old_text
        body["new_text"] = req.new_text
        result = await forward_to_sandbox(req.sandbox_id, "/file/edit", body)
    elif req.action == "append":
        body["content"] = req.content
        result = await forward_to_sandbox(req.sandbox_id, "/file/append", body)
    elif req.action == "view":
        result = await forward_to_sandbox(req.sandbox_id, "/file/view", body)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown file action: {req.action}")
    return result


# ──────────────────── Tool: Browser ────────────────────

class BrowserRequest(BaseModel):
    sandbox_id: str
    action: str = "navigate"
    url: str = ""
    click_id: int = 0
    text: str = ""
    direction: str = "down"
    amount: int = 500


@app.post("/tool/browser")
async def tool_browser(req: BrowserRequest, request: Request):
    verify_signature(request)
    body: dict[str, Any] = {}
    if req.action == "navigate":
        body["url"] = req.url
        result = await forward_to_sandbox(req.sandbox_id, "/browser/navigate", body, timeout=30)
    elif req.action == "click":
        body["click_id"] = req.click_id
        result = await forward_to_sandbox(req.sandbox_id, "/browser/click", body, timeout=30)
    elif req.action == "type":
        body["click_id"] = req.click_id
        body["text"] = req.text
        result = await forward_to_sandbox(req.sandbox_id, "/browser/type", body, timeout=30)
    elif req.action == "scroll":
        body["direction"] = req.direction
        body["amount"] = req.amount
        result = await forward_to_sandbox(req.sandbox_id, "/browser/scroll", body)
    elif req.action == "extract":
        result = await forward_to_sandbox(req.sandbox_id, "/browser/extract", body)
    elif req.action == "screenshot":
        result = await forward_to_sandbox(req.sandbox_id, "/browser/screenshot", body)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown browser action: {req.action}")
    return result


# ──────────────────── Tool: Match ────────────────────

class MatchRequest(BaseModel):
    sandbox_id: str
    action: str = "glob"
    pattern: str = ""
    path: str = "/root"
    include: str = ""


@app.post("/tool/match")
async def tool_match(req: MatchRequest, request: Request):
    verify_signature(request)
    body: dict[str, Any] = {"pattern": req.pattern, "path": req.path}
    if req.include:
        body["include"] = req.include
    if req.action == "glob":
        result = await forward_to_sandbox(req.sandbox_id, "/match/glob", body)
    elif req.action == "grep":
        result = await forward_to_sandbox(req.sandbox_id, "/match/grep", body)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown match action: {req.action}")
    return result


# ──────────────────── Tool: Web Search ────────────────────

class SearchRequest(BaseModel):
    query: str
    max_results: int = 5
    search_depth: str = "basic"


@app.post("/tool/search")
async def tool_search(req: SearchRequest, request: Request):
    verify_signature(request)
    if not TAVILY_API_KEY:
        raise HTTPException(status_code=500, detail="TAVILY_API_KEY not configured on proxy")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": req.query,
                "max_results": min(req.max_results, 10),
                "search_depth": req.search_depth,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    results = data.get("results", [])
    return {
        "query": req.query,
        "results": [
            {"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", "")[:500]}
            for r in results
        ],
        "answer": data.get("answer", ""),
    }


# ──────────────────── Tool: Image Generation ────────────────────

class GenerateRequest(BaseModel):
    sandbox_id: str | None = None
    action: str = "image"
    prompt: str = ""
    path: str = "/root/generated_image.png"
    image_path: str = ""


@app.post("/tool/generate")
async def tool_generate(req: GenerateRequest, request: Request):
    verify_signature(request)
    if req.action == "image":
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(RORK_IMAGE_URL, json={"prompt": req.prompt})
            resp.raise_for_status()
            data = resp.json()
        image_data = data.get("image", "") or data.get("data", "") or data.get("b64_json", "")
        if not image_data:
            raise HTTPException(status_code=502, detail=f"No image data. Keys: {list(data.keys())}")
        if req.sandbox_id:
            await forward_to_sandbox(
                req.sandbox_id, "/file/write",
                {"path": req.path, "content": image_data, "encoding": "base64"},
            )
            return {"status": "ok", "path": req.path, "message": f"Image generated and saved to {req.path}"}
        return {"status": "ok", "image_base64": image_data[:100] + "...", "message": "Generated (no sandbox_id)"}

    elif req.action == "edit":
        if not req.sandbox_id:
            raise HTTPException(status_code=400, detail="sandbox_id required for image editing")
        view_result = await forward_to_sandbox(req.sandbox_id, "/file/view", {"path": req.image_path})
        image_data = view_result.get("base64", "")
        if not image_data:
            raise HTTPException(status_code=404, detail=f"Could not read image: {req.image_path}")
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(RORK_IMAGE_EDIT_URL, json={"prompt": req.prompt, "image": image_data})
            resp.raise_for_status()
            data = resp.json()
        edited = data.get("image", "") or data.get("data", "") or data.get("b64_json", "")
        if not edited:
            raise HTTPException(status_code=502, detail="No image data in edit response")
        await forward_to_sandbox(
            req.sandbox_id, "/file/write",
            {"path": req.path, "content": edited, "encoding": "base64"},
        )
        return {"status": "ok", "path": req.path, "message": f"Image edited and saved to {req.path}"}
    else:
        raise HTTPException(status_code=400, detail=f"Unknown generate action: {req.action}")


# ──────────────────── Main ────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
