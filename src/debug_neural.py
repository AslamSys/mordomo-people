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
    """Accepts a full audio file and streams it into the pipeline in chunks."""
    try:
        audio_bytes = await file.read()
        logger.info(f"DEBUG: Received audio upload ({len(audio_bytes)} bytes). Streaming in chunks...")
        
        # 4KB chunks
        chunk_size = 4096
        for i in range(0, len(audio_bytes), chunk_size):
            chunk = audio_bytes[i:i + chunk_size]
            await nc.publish("mordomo.audio.stream", chunk)
            # Small delay to simulate real-time streaming and prevent NATS congestion
            await asyncio.sleep(0.01) 
            
        await nc.flush()
        return {"status": "ok", "bytes": len(audio_bytes), "chunks": (len(audio_bytes) // chunk_size) + 1}
    except Exception as e:
        logger.error(f"DEBUG: Streaming failed: {e}")
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
