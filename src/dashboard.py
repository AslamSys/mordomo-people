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
from starlette.middleware.sessions import SessionMiddleware
from passlib.context import CryptContext
from nats.aio.client import Client as NATS

# Internal imports
from .db import init_pool, close_pool, _pool_conn
from .config import VAULT_URL
from .debug_neural import app as debug_app

logger = logging.getLogger("mordomo-people")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

app = FastAPI(title="Mordomo Resident Hub")

# Session Secret (tries Vault first, then ENV, then random)
def get_session_secret():
    try:
        import requests
        resp = requests.get(f"{VAULT_URL}/get_all", timeout=1.0)
        if resp.status_code == 200:
            return resp.json().get("SESSION_SECRET", secrets.token_urlsafe(32))
    except: pass
    return os.getenv("SESSION_SECRET", secrets.token_urlsafe(32))

app.add_middleware(SessionMiddleware, secret_key=get_session_secret())
app.mount("/debug-neural", debug_app)
templates = Jinja2Templates(directory="src/templates")

# NATS Global Client
nc = NATS()

@app.on_event("startup")
async def startup_event():
    await init_pool()
    try:
        await nc.connect("nats://nats:4222")
        logger.info("NATS connected: nats://nats:4222")
        await seed_vault()
    except Exception as e:
        logger.error(f"NATS connection failed: {e}")

# Dependency to check authentication
def get_current_user(request: Request):
    user = request.session.get("user")
    if not user:
        return None
    return user

async def get_admin_count():
    async with _pool_conn() as conn:
        return await conn.fetchval("SELECT count(*) FROM people.pessoas WHERE is_owner = True")

async def get_system_status(request: Request):
    """Bridge to other services for health status."""
    return {
        "infra": {"nats": "online", "redis": "online", "postgres": "online", "vault": "online", "qdrant": "online"},
        "iot": {"mqtt": "online"},
        "brain": {"brain": "online", "bifrost": "online", "orchestrator": "online"},
        "audio": {"capture": "online", "asr": "online", "tts": "online"},
        "finance": {"finances": "online"}
    }

async def get_vault_health():
    essential_keys = [
        "GROQ_API_KEY", "BIFROST_API_KEY", "DATABASE_URL", "PEOPLE_MASTER_KEY",
        "OPENCLAW_GATEWAY_TOKEN"
    ]
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
    seeds = [
        {"key": "DATABASE_URL", "value": os.environ.get("DATABASE_URL")},
        {"key": "PEOPLE_MASTER_KEY", "value": os.environ.get("PEOPLE_MASTER_KEY")},
        {"key": "BIFROST_API_KEY", "value": "bt_" + secrets.token_urlsafe(24)},
        {"key": "SESSION_SECRET", "value": secrets.token_urlsafe(24)},
        {"key": "OPENCLAW_GATEWAY_TOKEN", "value": os.environ.get("OPENCLAW_GATEWAY_TOKEN")}
    ]
    existing_keys = {}
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{VAULT_URL}/get_all", timeout=2.0)
            if resp.status_code == 200:
                existing_keys = resp.json()
        except: pass
    for s in seeds:
        if not existing_keys.get(s["key"]) and s["value"]:
            async with httpx.AsyncClient() as client:
                await client.post(f"{VAULT_URL}/set", json=s)

@app.on_event("startup")
async def startup_event():
    import asyncio
    asyncio.create_task(seed_vault())

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, user: dict = Depends(get_current_user)):
    admin_count = await get_admin_count()
    if admin_count == 0:
        return templates.TemplateResponse(request=request, name="welcome.html", context={})
    if not user:
        login_mode = request.query_params.get("login_mode") == "true"
        return templates.TemplateResponse(request=request, name="welcome.html", context={"login_mode": login_mode})
    
    async with _pool_conn() as conn:
        rows = await conn.fetch("SELECT id, name, description, is_owner, whatsapp_number, voice_profile_id FROM people.pessoas ORDER BY name")
    residents = [dict(r) for r in rows]
    current_res = next((r for r in residents if r["id"] == user["id"]), None)
    
    if current_res:
        is_incomplete = not current_res.get("whatsapp_number") or not current_res.get("voice_profile_id") or not current_res.get("description")
        if is_incomplete:
            return RedirectResponse(url="/wizard?mode=persona&target=self")

    return templates.TemplateResponse(
        request=request, name="dashboard.html", 
        context={
            "residents": residents, 
            "system": await get_system_status(request), 
            "vault": await get_vault_health(), 
            "user": user
        }
    )

@app.post("/login")
async def login(request: Request, name: str = Form(...), password: str = Form(...)):
    async with _pool_conn() as conn:
        row = await conn.fetchrow("SELECT id, name, password_hash, is_owner FROM people.pessoas WHERE lower(name) = lower($1)", name)
        if not row or not row["password_hash"]:
            return RedirectResponse(url="/?login_mode=true&error=invalid", status_code=303)
        if not pwd_context.verify(password, row["password_hash"]):
            return RedirectResponse(url="/?login_mode=true&error=invalid", status_code=303)
        
        request.session["user"] = {"id": str(row["id"]), "name": row["name"], "is_owner": row["is_owner"]}
        return RedirectResponse(url="/", status_code=303)

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)

@app.get("/wizard", response_class=HTMLResponse)
async def wizard_page(request: Request, mode: str = "persona", target: str = "self", user: dict = Depends(get_current_user)):
    if not user: return RedirectResponse(url="/")
    return templates.TemplateResponse(request=request, name="wizard.html", context={"user": user, "mode": mode, "target": target})

@app.get("/openclaw-guide", response_class=HTMLResponse)
async def openclaw_guide_page(request: Request, user: dict = Depends(get_current_user)):
    if not user: return RedirectResponse(url="/")
    token = "Não Gerado"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{VAULT_URL}/get_all", timeout=2.0)
            if resp.status_code == 200:
                token = resp.json().get("OPENCLAW_GATEWAY_TOKEN", "Não Gerado (Reinicie o Orchestrator)")
    except:
        pass
    return templates.TemplateResponse(request=request, name="openclaw_guide.html", context={"token": token})

@app.post("/vault/save")
async def save_vault_keys(
    request: Request,
    groq_key: str = Form(None),
    user: dict = Depends(get_current_user)
):
    if not user or not user.get("is_owner"):
        return JSONResponse({"error": "unauthorized"}, status_code=status.HTTP_403_FORBIDDEN)
    
    async with httpx.AsyncClient() as client:
        if groq_key:
            await client.post(f"{VAULT_URL}/set", json={"key": "GROQ_API_KEY", "value": groq_key})

    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/vault/save_single")
async def save_single_vault_key(
    request: Request,
    key_name: str = Form(...),
    key_value: str = Form(...),
    user: dict = Depends(get_current_user)
):
    if not user or not user.get("is_owner"):
        return JSONResponse({"error": "unauthorized"}, status_code=status.HTTP_403_FORBIDDEN)
    
    if key_name and key_value:
        async with httpx.AsyncClient() as client:
            await client.post(f"{VAULT_URL}/set", json={"key": key_name, "value": key_value})
            
    return RedirectResponse(url="/#sect-vault", status_code=status.HTTP_303_SEE_OTHER)
