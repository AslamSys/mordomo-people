import json
import logging
import asyncio
import os
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from nats.aio.client import Client as NATS

logger = logging.getLogger("mordomo-debug")
app = FastAPI(title="Neural Pipeline Debugger")
templates = Jinja2Templates(directory="src/templates")

# Shared NATS client
_nc = NATS()

async def get_nc():
    if not _nc.is_connected:
        try:
            await _nc.connect("nats://nats:4222", pending_size=1024*1024*10)
            logger.info("DEBUG: NATS connected for pipeline monitor")
        except Exception as e:
            logger.error(f"DEBUG: NATS connection failed: {e}")
    return _nc

@app.get("/monitor", response_class=HTMLResponse)
async def monitor_page(request: Request):
    return templates.TemplateResponse(request=request, name="debug_audio.html", context={})

@app.websocket("/ws")
async def monitor_ws(websocket: WebSocket):
    await websocket.accept()
    logger.info("DEBUG_WS: Connection accepted (Full Pipeline Mode)")
    
    client = await get_nc()
    sub = None

    async def msg_handler(msg):
        try:
            subject = msg.subject
            data = msg.data
            
            # Identify content type
            content = None
            if subject.endswith(".energy"):
                content = json.loads(data.decode())
            elif subject.endswith(".speech") or subject.endswith(".stream"):
                # Audio blobs (limit volume to avoid WS flood if needed)
                content = {"type": "audio_event", "len": len(data)}
            else:
                try:
                    content = json.loads(data.decode())
                except:
                    content = data.decode()
            
            await websocket.send_json({"topic": subject, "payload": content})
        except Exception as e:
            pass

    if client.is_connected:
        try:
            # Subscribe to all telemetry
            sub = await client.subscribe("mordomo.>", cb=msg_handler)
            logger.info("DEBUG_WS: Subscribed to NATS telemetry portal")
        except Exception as e:
            logger.error(f"DEBUG_WS: Subscription failed: {e}")

    try:
        while True:
            data = await websocket.receive()
            
            if "text" in data:
                try:
                    msg = json.loads(data["text"])
                    topic = msg.get("topic")
                    payload = msg.get("payload", {})
                    
                    if topic and client.is_connected:
                        await client.publish(topic, json.dumps(payload).encode())
                        await client.flush()
                except:
                    # Legacy or raw text
                    text = data["text"]
                    if text.startswith("simulate:"):
                        cmd = text.replace("simulate:", "")
                        await client.publish("mordomo.orchestrator.request", json.dumps({"text": cmd, "user_id": "debug"}).encode())

            elif "bytes" in data and client.is_connected:
                # PUBLISH PC MIC TO VAD INPUT
                await client.publish("mordomo.audio.stream", data["bytes"])
                
    except WebSocketDisconnect:
        logger.warning("DEBUG_WS: Disconnected")
        if sub: await sub.unsubscribe()
    except Exception as e:
        logger.error(f"DEBUG_WS: Error: {e}")
        if sub: await sub.unsubscribe()
