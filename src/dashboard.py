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
import time
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
        "GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
        "BIFROST_API_KEY", "DATABASE_URL", "PEOPLE_MASTER_KEY", "OPENCLAW_GATEWAY_TOKEN"
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
        {"key": "OPENCLAW_GATEWAY_TOKEN", "value": os.environ.get("OPENCLAW_GATEWAY_TOKEN")},
        {"key": "GROQ_API_KEY", "value": os.environ.get("GROQ_API_KEY", "")},
        {"key": "OPENAI_API_KEY", "value": os.environ.get("OPENAI_API_KEY", "")},
        {"key": "ANTHROPIC_API_KEY", "value": os.environ.get("ANTHROPIC_API_KEY", "")},
        {"key": "GOOGLE_API_KEY", "value": os.environ.get("GOOGLE_API_KEY", "")},
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
        # If admins already exist, default to login mode unless explicitly requested otherwise
        default_login = admin_count > 0
        login_mode = request.query_params.get("login_mode", "true" if default_login else "false") == "true"
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

@app.post("/add")
async def add_first_user(request: Request, name: str = Form(...), password: str = Form(...), is_owner: bool = Form(False)):
    """Handles the first administrator/owner creation."""
    password_hash = pwd_context.hash(password)
    
    async with _pool_conn() as conn:
        # Check if any admin exists
        admin_count = await conn.fetchval("SELECT count(*) FROM people.pessoas WHERE is_owner = True")
        
        # If it's the first user, always allow owner. If not, only admins could add. 
        # But here we focus on the setup wizard.
        try:
            person_id = await conn.fetchval(
                """
                INSERT INTO people.pessoas (name, password_hash, is_owner)
                VALUES ($1, $2, $3)
                ON CONFLICT (lower(name)) DO NOTHING
                RETURNING id
                """,
                name, password_hash, is_owner
            )
            
            if person_id:
                # Auto-login the founder
                request.session["user"] = {"id": str(person_id), "name": name, "is_owner": is_owner}
                return RedirectResponse(url="/", status_code=303)
            else:
                # User already exists, redirect to login
                return RedirectResponse(url="/?login_mode=true&error=exists", status_code=303)
        except Exception as e:
            logger.error(f"Failed to add founder: {e}")
            return RedirectResponse(url="/?error=failed", status_code=303)

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
    return templates.TemplateResponse(
        request=request, name="wizard.html", 
        context={
            "user": user, 
            "mode": mode, 
            "target": target,
            "vault": await get_vault_health(),
            "request": request
        }
    )

@app.post("/people/update")
async def update_person(
    request: Request,
    id: str = Form(None),
    name: str = Form(None),
    description: str = Form(None),
    aliases: str = Form(None),
    whatsapp: str = Form(None),
    voice_profile_id: str = Form(None),
    is_owner: bool = Form(False),
    user: dict = Depends(get_current_user)
):
    if not user: return RedirectResponse(url="/", status_code=303)
    
    # If target is self, use session ID
    target_id = id or user["id"]
    
    # Only admins can update others
    if target_id != user["id"] and not user.get("is_owner"):
        raise HTTPException(status_code=403, detail="Unauthorized")

    async with _pool_conn() as conn:
        alias_list = [a.strip() for a in aliases.split(",")] if aliases else []
        await conn.execute(
            """
            UPDATE people.pessoas 
            SET name = COALESCE($1, name),
                description = COALESCE($2, description),
                aliases = $3,
                whatsapp_number = COALESCE($4, whatsapp_number),
                voice_profile_id = COALESCE($5, voice_profile_id),
                is_owner = COALESCE($6, is_owner)
            WHERE id = $7
            """,
            name, description, alias_list, whatsapp, voice_profile_id, is_owner, target_id
        )
    
    return RedirectResponse(url="/", status_code=303)

@app.post("/wizard/voice/enroll")
async def enroll_voice(request: Request, audio: UploadFile = File(...), user: dict = Depends(get_current_user)):
    if not user: return JSONResponse({"error": "unauthorized"}, status_code=403)
    # Placeholder for voice enrollment until audio pipeline is active
    # For now, return a mock voice_id
    mock_voice_id = f"voice_{secrets.token_hex(4)}"
    logger.info(f"Mock voice enrollment for user {user['name']}: {mock_voice_id}")
    return {"voice_id": mock_voice_id}

OPENCLAW_CONFIG_PATH = os.getenv("OPENCLAW_CONFIG_PATH", "/openclaw-data/openclaw.json")

OPENCLAW_PROVIDERS = {
    "openai":    {"name": "OpenAI",    "baseUrl": "https://api.openai.com/v1"},
    "anthropic": {"name": "Anthropic", "baseUrl": "https://api.anthropic.com/v1"},
    "groq":      {"name": "Groq",      "baseUrl": "https://api.groq.com/openai/v1"},
    "google":    {"name": "Google",    "baseUrl": "https://generativelanguage.googleapis.com/v1beta"},
}

# Simple in-memory cache for models {provider: {"models": [...], "expiry": timestamp}}
MODELS_CACHE = {}
CACHE_TTL = 3600  # 1 hour

@app.get("/fetch-models/{provider}")
async def fetch_provider_models(provider: str, api_key: str = None):
    """Dynamically fetch models from the provider API with caching."""
    # Define cache key before checking cache
    cache_key = f"{provider}:{api_key[:10] if api_key else 'vault'}"
    
    # Check cache first
    now = time.time()
    CACHE_TTL_SHORT = 300 
    if cache_key in MODELS_CACHE and MODELS_CACHE[cache_key]["expiry"] > now:
        return {"models": MODELS_CACHE[cache_key]["models"], "source": "cache"}

    if provider not in OPENCLAW_PROVIDERS:
        raise HTTPException(status_code=400, detail="Invalid provider")
    
    if not api_key or api_key.startswith("AQ."):
        # Try to get from vault
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{VAULT_URL}/get_all", timeout=2.0)
                if resp.status_code == 200:
                    vdata = resp.json()
                    if provider == "groq": api_key = vdata.get("GROQ_API_KEY")
                    elif provider == "openai": api_key = vdata.get("OPENAI_API_KEY")
                    elif provider == "google": api_key = vdata.get("GOOGLE_API_KEY")
                    elif provider == "anthropic": api_key = vdata.get("ANTHROPIC_API_KEY")
        except Exception as e:
            logger.warning(f"Vault lookup failed for {provider}: {e}")

    if not api_key or api_key.startswith("AQ."):
        return {"models": [], "source": "error", "message": "No API key found in Vault or request."}

    try:
        async with httpx.AsyncClient() as client:
            models_found = []
            if provider == "google":
                url = f"{OPENCLAW_PROVIDERS[provider]['baseUrl']}/models?key={api_key}"
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    raw_models = data.get("models", [])
                    for m in raw_models:
                        name = m.get("name", "").split("/")[-1]
                        methods = m.get("supportedGenerationMethods", [])
                        if "generateContent" in methods or name.startswith(("gemini", "gemma")):
                            models_found.append(name)
                else:
                    logger.error(f"Google API Error {resp.status_code}: {resp.text}")
            elif provider == "anthropic":
                url = f"{OPENCLAW_PROVIDERS[provider]['baseUrl']}/models"
                headers = {
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01"
                }
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    models_found = [m["id"] for m in data.get("data", [])]
                else:
                    logger.error(f"Anthropic API Error {resp.status_code}: {resp.text}")
            else:
                # OpenAI / Groq pattern
                url = f"{OPENCLAW_PROVIDERS[provider]['baseUrl']}/models"
                resp = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
                if resp.status_code == 200:
                    data = resp.json()
                    models_found = [m["id"] for m in data.get("data", []) or data.get("models", [])]
                else:
                    logger.error(f"API Error {resp.status_code} for {provider}: {resp.text}")
            
            if models_found:
                # 2026 Refined LLM Filter
                exclude = ["embedding", "aqa", "whisper", "tts", "embed", "guard", "moderation", "audio", "dall-e", "imagen", "veo"]
                filtered = [m for m in models_found if not any(x in m.lower() for x in exclude)]
                
                # Update cache
                MODELS_CACHE[cache_key] = {"models": sorted(filtered), "expiry": time.time() + CACHE_TTL_SHORT}
                return {"models": sorted(filtered), "source": "api"}
            
            return {"models": [], "source": "error", "message": f"API returned no models for {provider}."}

    except Exception as e:
        logger.error(f"Error fetching models for {provider}: {e}")
        return {"models": [], "source": "error", "message": str(e)}

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
    """Write or update openclaw.json with the selected provider, preserving other settings."""
    pdata = OPENCLAW_PROVIDERS[provider]
    base_url = pdata["baseUrl"]
    
    current_config = {}
    try:
        if os.path.exists(OPENCLAW_CONFIG_PATH):
            with open(OPENCLAW_CONFIG_PATH, "r") as f:
                content = f.read()
                # OpenClaw config might have comments or be JS-like, but we try to parse it as JSON
                # If it's the complex template we wrote, we might need a more robust parser or just keep it simple
                import json
                try:
                    current_config = json.loads(content)
                except:
                    # Fallback: if it's our template, it's not valid JSON. 
                    # For Zero-Touch, we'll move to a pure JSON structure now.
                    pass
    except:
        pass

    # Prepare surgical update
    base_url = pdata["baseUrl"]
    
    # Ensure intelligence structure
    if "models" not in current_config:
        current_config["models"] = {"providers": {}}
    if "providers" not in current_config["models"]:
        current_config["models"]["providers"] = {}
        
    # Surgical update of provider config
    if provider not in current_config["models"]["providers"]:
        current_config["models"]["providers"][provider] = {}
    
    current_config["models"]["providers"][provider].update({
        "baseUrl": base_url,
        "apiKey": api_key
    })

    # Ensure the selected model is at least in the list (OpenClaw UI compatibility)
    if "models" not in current_config["models"]["providers"][provider]:
        current_config["models"]["providers"][provider]["models"] = []
    
    existing_model_ids = [m["id"] for m in current_config["models"]["providers"][provider]["models"]]
    if model not in existing_model_ids:
        current_config["models"]["providers"][provider]["models"].append({"id": model, "name": model})
    
    if "agents" not in current_config:
        current_config["agents"] = {"defaults": {}}
    current_config["agents"]["defaults"]["model"] = f"{provider}/{model}"

    # Metadata
    if "meta" not in current_config:
        current_config["meta"] = {}
    current_config["meta"].update({
        "lastTouchedVersion": "2026.4.22",
        "lastTouchedAt": "2026-04-23T23:00:00Z"
    })

    # Save the merged config
    with open(OPENCLAW_CONFIG_PATH, "w") as f:
        import json
        json.dump(current_config, f, indent=2)
    
    # 2026 UPDATE: Do NOT wipe agents directory on every save.
    # Wiping agents causes WhatsApp and other persistent sessions to be lost.
    try:
        data_dir = os.path.dirname(OPENCLAW_CONFIG_PATH)
        workspace_path = os.path.join(data_dir, "workspace")
        agents_path = os.path.join(data_dir, "agents")
        
        if not os.path.exists(agents_path):
            logger.info(f"No agents directory to wipe at {agents_path}")
            
        # Clear workspace triggers and modular identity files (2026 pattern)
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
        
        # 1. SOUL.md - The Core Personality (Aslam Bridge version)
        soul_path = os.path.join(workspace_path, "SOUL.md")
        soul_text = """# OpenClaw SOUL (AslamSys)
Você é a interface neural do ecossistema **AslamSys**.

## Diretrizes de Resposta (ECONOMIA DE TOKENS):
- **Concisedness:** Responda de forma direta e curta. Elimine saudações automáticas e frases de cortesia desnecessárias.
- **No Fluff:** Não repita a pergunta do usuário. Apenas execute e confirme o resultado.
- **Foco Funcional:** Sua prioridade é a ação. Use o tom executivo e preciso.

## Conexão com o AslamSys:
Você é o portal para o **Mordomo Orchestrator**. 
Ações físicas (luzes, IoT) ou consultas locais (finanças, sistema) devem ser delegadas ao Mordomo.

### Ferramenta de Comando:
- **Endpoint:** http://mordomo-people:8000/api/command (POST)
- **Body:** { "text": "comando", "user_id": "...", "channel": "whatsapp" }

### Regras de Operação:
1. Você é o front-end; o Mordomo é o executor.
2. Repasse a resposta da API do Mordomo de forma resumida.
3. Use ferramentas de IA/Web apenas se o Mordomo não puder resolver localmente.

---
*Configurado via People Hub - Otimizado para Baixa Latência.*
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
    vault_status = {}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{VAULT_URL}/get_all", timeout=2.0)
            if resp.status_code == 200:
                vdata = resp.json()
                token = vdata.get("OPENCLAW_GATEWAY_TOKEN", "Não Gerado (Reinicie o Orchestrator)")
                # Detect which providers have keys
                vault_status = {
                    "groq": "GROQ_API_KEY" in vdata and bool(vdata["GROQ_API_KEY"]),
                    "openai": "OPENAI_API_KEY" in vdata and bool(vdata["OPENAI_API_KEY"]),
                    "google": "GOOGLE_API_KEY" in vdata and bool(vdata["GOOGLE_API_KEY"]),
                    "anthropic": "ANTHROPIC_API_KEY" in vdata and bool(vdata["ANTHROPIC_API_KEY"]),
                }
    except:
        pass

    config = _read_openclaw_config()
    return templates.TemplateResponse(request=request, name="openclaw_guide.html", context={
        "token": token,
        "providers": OPENCLAW_PROVIDERS,
        "current_config": config,
        "vault_status": vault_status,
        "request": request
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

    return JSONResponse({"status": "ok"})

    if key_name and key_value:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(f"{VAULT_URL}/set", json={"key": key_name, "value": key_value}, timeout=5.0)
                if resp.status_code == 200:
                    return JSONResponse({"status": "ok"})
                else:
                    return JSONResponse({"status": "error", "message": f"Vault returned {resp.status_code}"}, status_code=500)
        except Exception as e:
            logger.error(f"Vault save error: {e}")
            return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
    return JSONResponse({"status": "error", "message": "Missing key or value"}, status_code=400)
            
@app.post("/api/command")
async def api_command(request: Request):
    """
    Bridge for OpenClaw/External Agents to talk to the Mordomo Orchestrator.
    Expects: {"text": "...", "user_id": "...", "channel": "..."}
    """
    try:
        body = await request.json()
        text = body.get("text")
        user_id = body.get("user_id", "openclaw_agent")
        channel = body.get("channel", "openclaw")
        
        if not text:
            return JSONResponse({"error": "text is required"}, status_code=400)

        # Build payload for orchestrator
        payload = {
            "text": text,
            "user_id": user_id,
            "channel": channel,
            "session_id": f"api_{secrets.token_hex(4)}"
        }
        
        # Publish to NATS and wait for reply
        if not nc.is_connected:
            await nc.connect("nats://nats:4222")
            
        logger.info(f"API Bridge: Forwarding command to orchestrator: {text}")
        response_msg = await nc.request("mordomo.orchestrator.request", json.dumps(payload).encode(), timeout=10.0)
        
        result = json.loads(response_msg.data)
        return JSONResponse(result)
        
    except asyncio.TimeoutError:
        return JSONResponse({"error": "Orchestrator timeout"}, status_code=504)
    except Exception as e:
        logger.error(f"API Bridge Error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
