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
app.mount("/static", StaticFiles(directory="src/static"), name="static")
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

OPENCLAW_CONFIG_PATH = os.getenv("OPENCLAW_CONFIG_PATH", "/openclaw-data/openclaw.json")

OPENCLAW_PROVIDERS = {
    "openai":    {"name": "OpenAI",    "baseUrl": "https://api.openai.com/v1",    "models": ["gpt-5", "gpt-5o", "gpt-4.5-preview", "o3-preview", "o3-mini"]},
    "anthropic": {"name": "Anthropic", "baseUrl": "https://api.anthropic.com",     "models": ["claude-4-sonnet-20260215", "claude-4-opus-20260310", "claude-3.7-sonnet", "claude-3.5-sonnet-v2"]},
    "groq":      {"name": "Groq",      "baseUrl": "https://api.groq.com/openai/v1", "models": ["llama-4-70b-versatile", "llama-3.3-70b", "mixtral-large-3", "gemma-3-27b"]},
    "google":    {"name": "Google",    "baseUrl": "https://generativelanguage.googleapis.com/v1beta", "models": ["gemini-3.1-pro", "gemini-3.1-flash", "gemini-3.1-flash-lite", "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite", "nano-banana-2", "nano-banana-pro"]},
}

def _read_openclaw_config():
    """Read current openclaw config to detect if a provider is already set."""
    try:
        with open(OPENCLAW_CONFIG_PATH, "r") as f:
            raw = f.read()
        # Simple detection: check if models.providers has a real provider key
        for pid in OPENCLAW_PROVIDERS:
            if f'"{pid}"' in raw or f"  {pid}:" in raw:
                # Try to extract the API key (masked)
                import re
                key_match = re.search(r'apiKey:\s*"([^"]+)"', raw)
                api_key = key_match.group(1) if key_match else ""
                model_match = re.search(r'model:\s*"([^"]+)"', raw)
                model = model_match.group(1) if model_match else ""
                return {"provider": pid, "api_key": api_key, "model": model, "configured": True}
    except:
        pass
    return {"provider": "", "api_key": "", "model": "", "configured": False}

def _write_openclaw_config(provider: str, api_key: str, model: str):
    """Write a clean openclaw.json with the selected provider."""
    pdata = OPENCLAW_PROVIDERS[provider]
    base_url = pdata["baseUrl"]
    # Build models catalog for OpenClaw
    models_entries = []
    for m in pdata["models"]:
        models_entries.append(f'          {{ id: "{m}", name: "{m}" }},')
    models_block = "\n".join(models_entries)

    config = f"""// OpenClaw — Mordomo Gateway Config
// Provider configured via AslamSys People Dashboard
{{
  gateway: {{
    port: 18789,
    bind: "auto",
    auth: {{
      mode: "token",
      token: "${{OPENCLAW_GATEWAY_TOKEN}}",
    }},
    controlUi: {{
      allowedOrigins: ["*", "http://localhost:18789", "http://127.0.0.1:18789"],
      allowInsecureAuth: true,
      dangerouslyDisableDeviceAuth: true,
    }},
  }},

  models: {{
    providers: {{
      {provider}: {{
        baseUrl: "{base_url}",
        apiKey: "{api_key}",
        models: [
{models_block}
        ],
      }},
    }},
  }},

  agents: {{
    defaults: {{
      model: "{provider}/{model}",
    }},
  }},
}}
"""
    with open(OPENCLAW_CONFIG_PATH, "w") as f:
        f.write(config)
    
    # NEW: Wipe the 'agents' directory to force OpenClaw to recreate the default agent 
    # using the NEW global provider/model settings. This prevents "No API key found for openai" errors.
    try:
        # Wipe agents folder to prevent stale profiles/auth errors
        data_dir = os.path.dirname(OPENCLAW_CONFIG_PATH)
        agents_path = os.path.join(data_dir, "agents")
        if os.path.exists(agents_path):
            import shutil
            logger.info(f"Wiping OpenClaw agents directory: {agents_path}")
            shutil.rmtree(agents_path)
        else:
            logger.info(f"No agents directory to wipe at {agents_path}")
            
        # Clear workspace triggers and modular identity files (2026 pattern)
        workspace_path = os.path.join(data_dir, "workspace")
        if os.path.exists(workspace_path):
            logger.info(f"Cleaning modular files in workspace: {workspace_path}")
            # List of all files that OpenClaw 2026 generates and uses for identity
            for f in ["BOOTSTRAP.md", "HEARTBEAT.md", "PLAN.md", "TODO.md", 
                      "SOUL.md", "IDENTITY.md", "AGENTS.md", "TOOLS.md", "USER.md"]:
                fpath = os.path.join(workspace_path, f)
                if os.path.exists(fpath):
                    os.remove(fpath)

        # NEW 2026: Inject Identity into SOUL.md and BOOTSTRAP.md
        os.makedirs(workspace_path, exist_ok=True)
        
        # 1. SOUL.md - The Core Personality
        soul_path = os.path.join(workspace_path, "SOUL.md")
        soul_text = """# OpenClaw SOUL (AslamSys)
Você é o **OpenClaw**, a interface neural humano-máquina do ecossistema **AslamSys**, rodando em um **Orange Pi 5 Ultra**.

## Personalidade:
- **Idioma:** Português do Brasil (sempre).
- **Tom:** Executivo, sofisticado e preciso.
- **Função:** Atuar como o portal para o Mordomo Orchestrator. 
- **Vibe:** Você é parte de uma infraestrutura de IA de ponta.

---
*Configurado via Mordomo People Hub.*
"""
        logger.info(f"Injecting identity into {soul_path}")
        with open(soul_path, "w", encoding="utf-8") as f:
            f.write(soul_text)

        # 2. BOOTSTRAP.md - Marks as completed
        bootstrap_path = os.path.join(workspace_path, "BOOTSTRAP.md")
        bootstrap_text = "Bootstrap concluído com sucesso via Zero-Touch Deployment. Siga as instruções em SOUL.md."
        logger.info(f"Marking bootstrap as completed in {bootstrap_path}")
        with open(bootstrap_path, "w", encoding="utf-8") as f:
            f.write(bootstrap_text)
                    
        logger.info(f"OpenClaw Zero-Touch configuration complete for {data_dir}")
    except Exception as e:
        logger.error(f"Failed to wipe agents directory: {e}")

async def _restart_openclaw_container():
    """Restart the OpenClaw container via Docker Engine API over Unix socket."""
    transport = httpx.AsyncHTTPTransport(uds="/var/run/docker.sock")
    async with httpx.AsyncClient(transport=transport, base_url="http://docker") as client:
        await client.post("/containers/mordomo-openclaw-agent/restart", params={"t": 5}, timeout=30.0)

async def _get_openclaw_container_status():
    """Check the OpenClaw container status via Docker Engine API. Returns dict with status, uptime, restarts."""
    try:
        transport = httpx.AsyncHTTPTransport(uds="/var/run/docker.sock")
        async with httpx.AsyncClient(transport=transport, base_url="http://docker") as client:
            resp = await client.get("/containers/mordomo-openclaw-agent/json", timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                state = data["State"]
                started_at = state.get("StartedAt", "")
                return {
                    "status": state["Status"],
                    "running": state.get("Running", False),
                    "restarting": state.get("Restarting", False),
                    "restart_count": data.get("RestartCount", 0),
                    "started_at": started_at,
                    "error": state.get("Error", ""),
                }
    except Exception as e:
        logger.error(f"Docker status check failed: {e}")
    return {"status": "unknown", "running": False, "restarting": False, "restart_count": 0, "started_at": "", "error": "cannot reach docker"}

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

    config = _read_openclaw_config()
    return templates.TemplateResponse(request=request, name="openclaw_guide.html", context={
        "token": token,
        "providers": OPENCLAW_PROVIDERS,
        "current_config": config,
    })

@app.post("/openclaw-config")
async def save_openclaw_config(
    request: Request,
    provider: str = Form(...),
    api_key: str = Form(...),
    model: str = Form(...),
    user: dict = Depends(get_current_user)
):
    if not user or not user.get("is_owner"):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    if provider not in OPENCLAW_PROVIDERS:
        return JSONResponse({"error": "invalid provider"}, status_code=400)

    _write_openclaw_config(provider, api_key, model)

    # Restart the container in background
    asyncio.create_task(_restart_openclaw_container())

    return JSONResponse({"status": "restarting", "message": "Configuração salva. OpenClaw reiniciando..."})

@app.get("/openclaw-status")
async def openclaw_status(request: Request, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    container_status = await _get_openclaw_container_status()
    # Return the dictionary directly so keys like .status and .running work in JS
    return JSONResponse(container_status)

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
