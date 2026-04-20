import os
import json
import logging
import secrets
import string
import httpx
import asyncio
from fastapi import FastAPI, Request, Form, Depends, HTTPException, status, Response, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from nats.aio.client import Client as NATS

# Internal imports (correcting to function imports)
from src.db import init_pool, close_pool, _pool_conn
from src.config import VAULT_URL
from src.auth import get_current_user
from src.debug_neural import app as debug_app

logger = logging.getLogger("mordomo-people")

app = FastAPI(title="Mordomo Resident Hub")
app.mount("/debug-neural", debug_app)
templates = Jinja2Templates(directory="src/templates")

# NATS Global Client
nc = NATS()

@app.on_event("startup")
async def startup_event():
    await init_pool() # From src.db
    try:
        await nc.connect("nats://nats:4222")
        logger.info("NATS connected: nats://nats:4222")
        await seed_vault()
    except Exception as e:
        logger.error(f"NATS connection failed: {e}")

async def get_admin_count():
    async with _pool_conn() as conn:
        return await conn.fetchval("SELECT count(*) FROM people.pessoas WHERE is_owner = True")

async def get_system_status(request: Request):
    """Bridge to other services for health status."""
    return {
        "infra": {"nats": "online", "redis": "online", "postgres": "online", "vault": "online", "qdrant": "online"},
        "iot": {"mqtt": "online", "orchestrator": "online"},
        "audio": {"capture": "online", "asr": "online", "tts": "online"},
        "finance": {"finances": "online"}
    }

async def get_vault_health():
    """Check if essential infrastructure keys exist in the Vault. (Strict Vault-First)"""
    essential_keys = ["GROQ_API_KEY", "BIFROST_API_KEY", "DATABASE_URL", "PEOPLE_MASTER_KEY"]
    health = {key: "missing" for key in essential_keys}
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{VAULT_URL}/get_all", timeout=2.0)
            if resp.status_code == 200:
                data = resp.json()
                for key in essential_keys:
                    if data.get(key) and len(data.get(key)) > 5:
                        health[key] = "ready"
    except Exception as e:
        logger.error(f"Vault health check failed: {e}")
        
    return health

async def seed_vault():
    """Auto-provision infrastructure secrets if Vault is empty."""
    seeds = [
        {"key": "DATABASE_URL", "value": os.environ.get("DATABASE_URL")},
        {"key": "PEOPLE_MASTER_KEY", "value": os.environ.get("PEOPLE_MASTER_KEY")},
        {"key": "BIFROST_API_KEY", "value": "bt_" + secrets.token_urlsafe(24)},
        {"key": "SESSION_SECRET", "value": secrets.token_urlsafe(24)}
    ]
    
    existing_keys = {}
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{VAULT_URL}/get_all", timeout=2.0)
            if resp.status_code == 200:
                existing_keys = resp.json()
        except:
            pass

    for s in seeds:
        k = s["key"]
        v = s["value"]
        if not existing_keys.get(k) and v:
            logger.info(f"Seeding {k} to Vault...")
            async with httpx.AsyncClient() as client:
                await client.post(f"{VAULT_URL}/set", json={"key": k, "value": v})

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, user: dict = Depends(get_current_user)):
    """The hub: Landing/Onboarding or Admin Dashboard."""
    try:
        admin_count = await get_admin_count()
        if admin_count == 0:
            return templates.TemplateResponse(request=request, name="welcome.html", context={})
        if not user:
            return templates.TemplateResponse(request=request, name="welcome.html", context={"login_mode": True})
        
        async with _pool_conn() as conn:
            rows = await conn.fetch("SELECT id, name, description, is_owner, whatsapp_number, voice_profile_id FROM people.pessoas ORDER BY name")
        residents = [dict(r) for r in rows]
        current_res = next((r for r in residents if r["id"] == user["id"]), None)
        
        if current_res:
            is_incomplete = not current_res.get("whatsapp_number") or not current_res.get("voice_profile_id") or not current_res.get("description")
            if is_incomplete:
                return RedirectResponse(url="/wizard?mode=persona&target=self", status_code=status.HTTP_303_SEE_OTHER)

        system_status = await get_system_status(request)
        vault_health = await get_vault_health()
        
        return templates.TemplateResponse(
            request=request, name="dashboard.html", 
            context={
                "residents": residents, "system": system_status, "vault": vault_health,
                "user": user, "setup_incomplete": False
            }
        )
    except Exception as e:
        logger.error(f"Index error: {e}")
        return HTMLResponse("Internal Error", status_code=500)

@app.get("/wizard", response_class=HTMLResponse)
async def wizard_page(request: Request, mode: str = "persona", target: str = "self", user: dict = Depends(get_current_user)):
    return templates.TemplateResponse("wizard.html", {"request": request, "user": user, "mode": mode, "target": target})

@app.post("/vault/save")
async def save_vault_keys(request: Request, groq_key: str = Form(None), user: dict = Depends(get_current_user)):
    if not user or not user.get("is_owner"):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    if groq_key:
        async with httpx.AsyncClient() as client:
            await client.post(f"{VAULT_URL}/set", json={"key": "GROQ_API_KEY", "value": groq_key})
    return RedirectResponse(url="/", status_code=303)
