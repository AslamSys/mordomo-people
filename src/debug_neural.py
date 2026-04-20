import json
import logging
import asyncio
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, UploadFile, File
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
    return templates.TemplateResponse(request=request, name="debug_audio.html", context={})

@app.post("/upload-audio")
async def upload_audio(file: UploadFile = File(...)):
    """Accepts a full audio file and pushes it to the voice pipeline."""
    try:
        audio_bytes = await file.read()
        logger.info(f"DEBUG: Received audio upload ({len(audio_bytes)} bytes). Publishing to NATS.")
        # Flush to ensure buffer doesn't overflow
        await nc.publish("mordomo.audio.stream", audio_bytes)
        await nc.flush()
        return {"status": "ok", "bytes": len(audio_bytes)}
    except Exception as e:
        logger.error(f"DEBUG: Upload failed: {e}")
        return {"status": "error", "message": str(e)}

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
            # Handle both JSON commands and RAW AUDIO
            data = await websocket.receive()
            
            if "text" in data:
                text = data["text"]
                if text.startswith("simulate:"):
                    cmd = text.replace("simulate:", "")
                    await nc.publish("mordomo.orchestrator.request", json.dumps({"text": cmd, "user_id": "debug"}).encode())
            
            elif "bytes" in data:
                # Raw audio chunk from browser
                await nc.publish("mordomo.audio.stream", data["bytes"])

    except WebSocketDisconnect:
        await sub.unsubscribe()
    except Exception:
        await sub.unsubscribe()
