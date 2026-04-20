import json
import logging
import asyncio
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from nats.aio.client import Client as NATS

logger = logging.getLogger("mordomo-debug")
app = FastAPI(title="Neural Pipeline Debugger")
templates = Jinja2Templates(directory="src/templates")

# NATS Client for Debugging
nc = NATS()

@app.on_event("startup")
async def startup_event():
    try:
        await nc.connect("nats://nats:4222")
        logger.info("DEBUG: NATS connected for pipeline monitor")
    except Exception as e:
        logger.error(f"DEBUG: NATS failed: {e}")

@app.get("/monitor", response_class=HTMLResponse)
async def monitor_page(request: Request):
    # This serves the same debug_audio.html
    return templates.TemplateResponse("debug_audio.html", {"request": request})

@app.websocket("/ws")
async def monitor_ws(websocket: WebSocket):
    await websocket.accept()
    
    async def msg_handler(msg):
        try:
            content = msg.data.decode()
            try:
                content = json.loads(content)
            except:
                pass
            await websocket.send_json({"topic": msg.subject, "payload": content})
        except:
            pass

    sub = await nc.subscribe("mordomo.>", cb=msg_handler)
    
    try:
        while True:
            # Keep alive and listen for simulated requests from client
            data = await websocket.receive_text()
            if data.startswith("simulate:"):
                text = data.replace("simulate:", "")
                await nc.publish("mordomo.orchestrator.request", json.dumps({"text": text, "user_id": "debug"}).encode())
    except WebSocketDisconnect:
        await sub.unsubscribe()
    except Exception:
        await sub.unsubscribe()
