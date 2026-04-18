from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from src import db
import logging

logger = logging.getLogger(__name__)

app = FastAPI(title="Mordomo Identity Manager")

# Setup templates
templates = Jinja2Templates(directory="src/templates")

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Render the dashboard home with the list of residents."""
    try:
        logger.info("Dashboard: Fetching residents...")
        async with db._pool_conn() as conn:
            rows = await conn.fetch("SELECT id, name, description, is_owner FROM people.pessoas ORDER BY name")
        
        residents = []
        for r in rows:
            residents.append({
                "id": str(r["id"]),
                "name": r["name"],
                "description": r["description"],
                "is_owner": r["is_owner"]
            })
            
        # Corrected signature for TemplateResponse in FastAPI 0.108+ / Starlette 0.32+
        return templates.TemplateResponse(
            request=request, 
            name="index.html", 
            context={"residents": residents}
        )
    except Exception as e:
        logger.exception("Dashboard: Failed to render index")
        return HTMLResponse(content=f"Error: {e}", status_code=500)

@app.get("/add", response_class=HTMLResponse)
async def add_page(request: Request):
    """Render the add resident form."""
    return templates.TemplateResponse(request=request, name="add.html", context={})

@app.post("/add")
async def add_resident(
    request: Request, # Need request for TemplateResponse even on POST if rendering
    name: str = Form(...),
    description: str = Form(""),
    is_owner: bool = Form(False),
    whatsapp: str = Form(None)
):
    """Handle new resident creation."""
    try:
        async with db._pool_conn() as conn:
            async with conn.transaction():
                # 1. Insert into people.pessoas
                person_id = await conn.fetchval(
                    "INSERT INTO people.pessoas (name, description, is_owner) VALUES ($1, $2, $3) RETURNING id",
                    name, description, is_owner
                )
                
                # 2. Add WhatsApp if provided
                if whatsapp:
                    await conn.execute(
                        "INSERT INTO people.contatos (person_id, type, value_enc, label) VALUES ($1, 'whatsapp', $2, 'Principal')",
                        person_id, db.encrypt(whatsapp)
                    )
        
        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        logger.error(f"Error adding resident: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/health")
async def health():
    return {"status": "ok", "service": "mordomo-people-ui"}
