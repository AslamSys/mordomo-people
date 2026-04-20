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

async def get_nc():
    """Ensures NATS is connected, especially when mounted as sub-app."""
    if not nc.is_connected:
        try:
            # Increase pending_size to 10MB to avoid "buffer limit exceeded"
            await nc.connect("nats://nats:4222", pending_size=1024*1024*10)
            logger.info("DEBUG: NATS connected for pipeline monitor (On-demand)")
        except Exception as e:
            logger.error(f"DEBUG: NATS connection failed: {e}")
    return nc

@app.get("/monitor", response_class=HTMLResponse)
async def monitor_page(request: Request):
    return templates.TemplateResponse(request=request, name="debug_audio.html", context={})

@app.post("/upload-audio")
async def upload_audio(file: UploadFile = File(...)):
    """Accepts a full audio file and streams it into the pipeline in chunks."""
    try:
        audio_bytes = await file.read()
        logger.info(f"DEBUG: Received audio upload ({len(audio_bytes)} bytes). Streaming...")
        
        client = await get_nc()
        if not client.is_connected:
            return {"status": "error", "message": "NATS not connected"}

        chunk_size = 4096
        for i in range(0, len(audio_bytes), chunk_size):
            chunk = audio_bytes[i:i + chunk_size]
            await client.publish("mordomo.audio.stream", chunk)
            await asyncio.sleep(0.01) 
            
        await client.flush()
        return {"status": "ok", "bytes": len(audio_bytes)}
    except Exception as e:
        logger.error(f"DEBUG: Streaming failed: {e}")
        return {"status": "error", "message": str(e)}

@app.websocket("/ws")
async def monitor_ws(websocket: WebSocket):
    await websocket.accept()
    client = await get_nc()
    
    async def msg_handler(msg):
        try:
            content = msg.data.decode()
            try: content = json.loads(content)
            except: pass
            await websocket.send_json({"topic": msg.subject, "payload": content})
        except: pass

    sub = None
    if client.is_connected:
        sub = await client.subscribe("mordomo.>", cb=msg_handler)
    
    try:
        while True:
            data = await websocket.receive()
            if "text" in data and data["text"].startswith("simulate:"):
                cmd = data["text"].replace("simulate:", "")
                if client.is_connected:
                    await client.publish("mordomo.orchestrator.request", json.dumps({"text": cmd, "user_id": "debug"}).encode())
            elif "bytes" in data and client.is_connected:
                await client.publish("mordomo.audio.stream", data["bytes"])
    except WebSocketDisconnect:
        if sub: await sub.unsubscribe()
    except Exception:
        if sub: await sub.unsubscribe()
