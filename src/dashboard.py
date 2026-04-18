from fastapi import FastAPI, Request, Form, Depends, HTTPException, status, Response
from fastapi.responses import HTMLResponse, RedirectResponse
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

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, user: dict = Depends(get_current_user)):
    """The hub: Landing/Onboarding or Admin Dashboard."""
    try:
        admin_count = await get_admin_count()
        
        # Scenario 1: No admins exist -> Force Onboarding (Welcome Page)
        if admin_count == 0:
            return templates.TemplateResponse(request=request, name="welcome.html", context={})
        
        # Scenario 2: Admin exists but not logged in -> Show Welcome/Login
        if not user:
            # We can pass an 'auth_required' flag if we want to show the login form instead of onboarding
            return templates.TemplateResponse(request=request, name="welcome.html", context={"login_mode": True})
        
        # Scenario 3: Logged in -> Show Dashboard
        async with db._pool_conn() as conn:
            rows = await conn.fetch("SELECT id, name, description, is_owner FROM people.pessoas ORDER BY name")
        residents = [dict(r) for r in rows]
        
        return templates.TemplateResponse(
            request=request, 
            name="dashboard.html", 
            context={"residents": residents, "admin_count": admin_count, "user": user}
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

@app.post("/add")
async def add_resident(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    is_owner: bool = Form(False),
    whatsapp: str = Form(None),
    password: str = Form(None)
):
    """Handle resident creation (Wizard or Dashboard)."""
    try:
        # Check if first admin
        admin_count = await get_admin_count()
        if admin_count == 0:
            is_owner = True # First user is always owner
            
        # Hashing without truncation
        hashed_pw = pwd_context.hash(password) if password else None
        
        async with db._pool_conn() as conn:
            async with conn.transaction():
                person_id = await conn.fetchval(
                    "INSERT INTO people.pessoas (name, description, is_owner, password_hash) VALUES ($1, $2, $3, $4) RETURNING id",
                    name, description, is_owner, hashed_pw
                )
                if whatsapp:
                    await conn.execute(
                        "INSERT INTO people.contatos (person_id, type, value_enc, label) VALUES ($1, 'whatsapp', $2, 'Principal')",
                        person_id, db.encrypt(whatsapp)
                    )
        
        # If was onboarding, log them in automatically
        if admin_count == 0:
            request.session["user"] = {"id": str(person_id), "name": name, "is_owner": True}
            
        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        logger.error(f"Error adding resident: {e}")
        return RedirectResponse(url="/?error=creation_failed", status_code=303)

@app.get("/welcome", response_class=HTMLResponse)
async def welcome_direct(request: Request):
    return templates.TemplateResponse(request=request, name="welcome.html", context={})
