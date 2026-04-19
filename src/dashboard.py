from fastapi import FastAPI, Request, Form, Depends, HTTPException, status, Response, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from passlib.context import CryptContext
from src import db, config
import logging
import os
import httpx

logger = logging.getLogger(__name__)

# Password hashing context - using sha256_crypt for better stability and no length issues
pwd_context = CryptContext(schemes=["sha256_crypt"], deprecated="auto")

app = FastAPI(title="Mordomo HQ | Management Portal")

# Add Session Middleware (Secret should ideally come from Vault)
# For now using the MASTER_KEY or a default.
SESSION_SECRET = os.getenv("SESSION_SECRET", "super-secret-mordomo-key")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

# Setup templates
templates = Jinja2Templates(directory="src/templates")
app.mount("/static", StaticFiles(directory="src/static"), name="static")

# Helper to check if an admin exists
async def get_admin_count():
    async with db._pool_conn() as conn:
        return await conn.fetchval("SELECT count(*) FROM people.pessoas WHERE is_owner = true")

# Dependency to check authentication
def get_current_user(request: Request):
    user = request.session.get("user")
    if not user:
        return None
    return user

from src.config import VAULT_URL, VAULT_TOKEN

async def get_vault_health():
    """Check if essential infrastructure keys exist in the Vault."""
    essential_keys = ["GROQ_API_KEY", "BIFROST_API_KEY"]
    health = {key: "missing" for key in essential_keys}
    
    if not VAULT_TOKEN:
        return health # Cannot check without token

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{VAULT_URL}/get_all", # Using the /get_all endpoint for simplicity
                timeout=2.0
            )
            if resp.status_code == 200:
                data = resp.json() # Returns {key: val}
                for key in essential_keys:
                    if data.get(key) and len(data.get(key)) > 5:
                        health[key] = "ready"
    except Exception as e:
        logger.error(f"Vault health check failed: {e}")
        
    return health

async def get_system_status(request: Request):
    """Health probes grouped by ecosystem."""
    status = {
        "infra": {
            "nats": "offline",
            "redis": "offline",
            "postgres": "offline",
            "qdrant": "offline",
            "vault": "offline"
        },
        "brain": {
            "bifrost": "offline",
            "orchestrator": "offline"
        },
        "audio": {
            "capture": "offline",
            "pipeline": "offline"
        },
        "iot": {
            "mqtt": "offline",
            "devices": "offline"
        },
        "finance": {
            "finances": "offline"
        }
    }
    
    # ── Infrastructure ───────────────────────
    try:
        if request.app.state.nc.is_connected: status["infra"]["nats"] = "online"
    except: pass
    try:
        if await request.app.state.redis.ping(): status["infra"]["redis"] = "online"
    except: pass
    try:
        async with db._pool_conn() as conn:
            if await conn.fetchval("SELECT 1"): status["infra"]["postgres"] = "online"
    except: pass
    try:
        async with httpx.AsyncClient() as client:
            if (await client.get("http://qdrant:6333", timeout=1.0)).status_code == 200:
                status["infra"]["qdrant"] = "online"
    except: pass
    try:
        async with httpx.AsyncClient() as client:
            if (await client.get(f"{VAULT_URL}/v1/sys/health", timeout=1.0)).status_code == 200:
                status["infra"]["vault"] = "online"
    except: pass

    # ── Brain ────────────────────────────────
    try:
        async with httpx.AsyncClient() as client:
            if (await client.get("http://llm-gateway:8080/", timeout=1.0)).status_code == 200:
                status["brain"]["bifrost"] = "online"
    except: pass
    status["brain"]["orchestrator"] = "online" if status["infra"]["nats"] == "online" else "offline"

    # ── Audio/IoT/Finance ────────────────────
    bus_ok = status["infra"]["nats"] == "online"
    status["audio"]["capture"] = "online" if bus_ok else "offline"
    status["audio"]["pipeline"] = "online" if bus_ok else "offline"
    status["iot"]["mqtt"] = "online" if bus_ok else "offline"
    status["finance"]["finances"] = "online" if bus_ok else "offline"

    return status


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, user: dict = Depends(get_current_user)):
    """The hub: Landing/Onboarding or Admin Dashboard."""
    try:
        admin_count = await get_admin_count()
        
        if admin_count == 0:
            return templates.TemplateResponse(request=request, name="welcome.html", context={})
        
        if not user:
            return templates.TemplateResponse(request=request, name="welcome.html", context={"login_mode": True})
        
        # Scenario 3: Logged in -> Show Dashboard
        async with db._pool_conn() as conn:
            rows = await conn.fetch("SELECT id, name, description, is_owner, whatsapp_number, voice_profile_id FROM people.pessoas ORDER BY name")
        residents = [dict(r) for r in rows]
        
        # Check if CURRENT user setup is complete
        current_res = next((r for r in residents if r["id"] == user["id"]), None)
        setup_incomplete = False
        if current_res and current_res["is_owner"]:
            if not current_res.get("whatsapp_number") or not current_res.get("voice_profile_id"):
                setup_incomplete = True

        system_status = await get_system_status(request)
        vault_health = await get_vault_health()
        
        return templates.TemplateResponse(
            request=request, 
            name="dashboard.html", 
            context={
                "residents": residents, 
                "admin_count": admin_count, 
                "user": user, 
                "system": system_status,
                "vault": vault_health,
                "setup_incomplete": setup_incomplete
            }
        )
    except Exception as e:
        logger.exception("Dashboard: Failed")
        return HTMLResponse(content=f"Error: {e}", status_code=500)

@app.post("/login")
async def login(request: Request, name: str = Form(...), password: str = Form(...)):
    """Authenticate an administrator."""
    async with db._pool_conn() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, password_hash, is_owner FROM people.pessoas WHERE lower(name) = lower($1)",
            name
        )
        if not row or not row["password_hash"]:
            return RedirectResponse(url="/?error=invalid_credentials", status_code=303)
        
        if not pwd_context.verify(password, row["password_hash"]):
            return RedirectResponse(url="/?error=invalid_credentials", status_code=303)
            
        # Set session
        request.session["user"] = {"id": str(row["id"]), "name": row["name"], "is_owner": row["is_owner"]}
        return RedirectResponse(url="/", status_code=303)

@app.get("/logout")
async def logout(request: Request):
    """Clear session and redirect."""
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)

@app.post("/vault/save")
@app.post("/vault/save")
async def save_vault_keys(
    request: Request,
    groq_key: str = Form(None),
    user: dict = Depends(get_current_user)
):
    if not user or not user.get("is_owner"):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
        
    import secrets
    import string
    
    # Check existing keys first
    existing_keys = {}
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{VAULT_URL}/get_all", timeout=2.0)
            if resp.status_code == 200:
                existing_keys = resp.json()
        except: pass

    # Prepare keys_to_save
    keys_to_save = {}
    if groq_key:
        keys_to_save["GROQ_API_KEY"] = groq_key
    
    # AUTO-GENERATE BIFROST KEY IF MISSING
    if not existing_keys.get("BIFROST_API_KEY"):
        alphabet = string.ascii_letters + string.digits
        new_key = "bt_" + ''.join(secrets.choice(alphabet) for _ in range(32))
        keys_to_save["BIFROST_API_KEY"] = new_key
    
    success_count = 0
    async with httpx.AsyncClient() as client:
        for k, v in keys_to_save.items():
            if v and len(v) > 5:
                try:
                    await client.post(
                        f"{VAULT_URL}/set",
                        json={"key": k, "value": v},
                        timeout=5.0
                    )
                    success_count += 1
                except Exception as e:
                    logger.error(f"Vault save error for {k}: {e}")

    return JSONResponse({"status": "ok", "saved": success_count, "bifrost_generated": "BIFROST_API_KEY" in keys_to_save})

@app.post("/add")
async def add_resident(
    id: str = Form(None),
    name: str = Form(...),
    description: str = Form(""),
    is_owner: bool = Form(False),
    whatsapp: str = Form(None),
    aliases: str = Form(""),
    voice_profile_id: str = Form(None),
    password: str = Form(None),
    user: dict = Depends(get_current_user)
):
    if not user:
        return RedirectResponse(url="/", status_code=303)
        
    try:
        alias_list = [a.strip() for a in aliases.split(",")] if aliases else []
        hashed_pw = pwd_context.hash(password) if password else None
        
        async with db._pool_conn() as conn:
            if id:
                await conn.execute("""
                    UPDATE people.pessoas 
                    SET name = $1, description = $2, is_owner = $3, 
                        whatsapp_number = $4, aliases = $5, voice_profile_id = $6,
                        updated_at = NOW()
                    WHERE id = $7
                """, name, description, is_owner, whatsapp, alias_list, voice_profile_id, id)
                if hashed_pw:
                    await conn.execute("UPDATE people.pessoas SET password_hash = $1 WHERE id = $2", hashed_pw, id)
            else:
                await conn.execute("""
                    INSERT INTO people.pessoas (name, description, is_owner, whatsapp_number, aliases, voice_profile_id, password_hash)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                """, name, description, is_owner, whatsapp, alias_list, voice_profile_id, hashed_pw)
                
        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        logger.error(f"Error adding resident: {e}")
        return RedirectResponse(url="/?error=creation_failed", status_code=303)

@app.get("/welcome", response_class=HTMLResponse)
async def welcome_direct(request: Request):
    return templates.TemplateResponse(request=request, name="welcome.html", context={})

@app.get("/wizard", response_class=HTMLResponse)
async def resident_wizard(
    request: Request, 
    target: str = "new", 
    mode: str = "persona", 
    user: dict = Depends(get_current_user)
):
    """The Step-by-Step Onboarding (Persona or Core)."""
    if not user:
        return RedirectResponse(url="/", status_code=303)
    
    # Fetch full user data from DB if target=self
    full_user = user
    if target == "self":
        async with db._pool_conn() as conn:
            row = await conn.fetchrow("SELECT id, name, aliases, description, whatsapp_number, voice_profile_id, is_owner FROM people.pessoas WHERE id = $1", user["id"])
            if row:
                full_user = dict(row)
                full_user["id"] = str(full_user["id"])

    vault_health = await get_vault_health()

    return templates.TemplateResponse(
        request=request, 
        name="wizard.html", 
        context={
            "user": full_user, 
            "target": target, 
            "mode": mode,
            "vault": vault_health
        }
    )
@app.post("/wizard/voice/enroll")
async def voice_enroll(
    request: Request,
    audio: UploadFile = File(...),
    user: dict = Depends(get_current_user)
):
    if not user:
        return JSONResponse(content={"error": "unauthorized"}, status_code=401)
    
    audio_bytes = await audio.read()
    logger.info(f"Received voice enrollment from {user['name']} (Size: {len(audio_bytes)} bytes)")
    
    # ── Bridge to NATS ──────────────────────
    # Other services (audio-pipeline) can listen to this and generate the real print
    try:
        enrollment_event = {
            "user_id": user["id"],
            "user_name": user["name"],
            "action": "enroll_voice",
            "format": "wav"
        }
        # Publish audio as raw bytes with metadata in headers
        await request.app.state.nc.publish(
            "mordomo.audio.enrollment",
            audio_bytes,
            headers={"x-mordomo-meta": str(enrollment_event)}
        )
    except Exception as e:
        logger.error(f"Failed to publish enrollment to NATS: {e}")

    # Generate the ID that will be used in the User DB
    voice_id = f"vprofile_{user['id'][:8]}"
    
    return {"voice_id": voice_id}
