import json
import logging
import asyncio
import time
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
    """Accepts full audio, simulates activation sequence, and streams it."""
    try:
        audio_bytes = await file.read()
        logger.info(f"DEBUG: Received audio upload ({len(audio_bytes)} bytes). Starting full simulation...")
        
        client = await get_nc()
        if not client.is_connected:
            return {"status": "error", "message": "NATS not connected"}

        # 1. Generate Fake Session
        session_id = f"debug-session-{int(time.time())}"
        
        # 2. Simulate Wake Word (Wait for ASR and Bio to prepare)
        await client.publish("mordomo.wake_word.detected", json.dumps({
            "timestamp": time.time(),
            "confidence": 0.99,
            "keyword": "mordomo",
            "session_id": session_id
        }).encode())
        await asyncio.sleep(0.1)

        # 3. Stream Audio (Real services will handle verification now)
        await asyncio.sleep(0.1)

        # 4. Stream Raw PCM Audio
        chunk_size = 4096
        for i in range(0, len(audio_bytes), chunk_size):
            chunk = audio_bytes[i:i + chunk_size]
            await client.publish("mordomo.audio.stream", chunk)
            await asyncio.sleep(0.01) 
            
        await client.flush()
        return {"status": "ok", "bytes": len(audio_bytes), "session_id": session_id}
    except Exception as e:
        logger.error(f"DEBUG: Simulation failed: {e}")
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

    # sub = None
    # if client.is_connected:
    #     try:
    #         sub = await client.subscribe("mordomo.>", cb=msg_handler)
    #         logger.info("DEBUG_WS: Subscribed to NATS mordomo.>")
    #     except Exception as e:
    #         logger.error(f"DEBUG_WS: NATS Sub error: {e}")
    
    try:
        while True:
            data = await websocket.receive()
            if "text" in data:
                text = data["text"]
                logger.info(f"DEBUG_WS: Received text: {text[:100]}")
                
                # Check if it is JSON (New standard)
                try:
                    msg = json.loads(text)
                    topic = msg.get("topic")
                    if topic and client.is_connected:
                        payload = msg.get("payload", {})
                        
                        # If simulating from input, map to orchestrator
                        if topic == "mordomo.debug.simulate":
                            target_topic = "mordomo.orchestrator.request"
                            out_msg = json.dumps({
                                "text": payload.get("text", ""),
                                "user_id": "debug",
                                "source": "monitor"
                            })
                        else:
                            target_topic = topic
                            out_msg = json.dumps(payload)
                        
                        logger.info(f"DEBUG_WS: Publishing to {target_topic} -> {out_msg[:50]}")
                        await client.publish(target_topic, out_msg.encode())
                        await client.flush()
                        logger.info(f"DEBUG_WS: Publish SUCCESS")
                    else:
                        logger.warning(f"DEBUG_WS: Missing topic or NATS disconnected (topic={topic}, connected={client.is_connected})")
                    continue
                except Exception as e:
                    logger.debug(f"DEBUG_WS: Not a standard JSON msg, checking fallback strings... ({e})")

                # Legacy String Parser (Fallback)
                if text.startswith("simulate:"):
                    cmd = text.replace("simulate:", "")
                    if client.is_connected:
                        logger.info(f"DEBUG_WS: Scaling legacy simulate: {cmd}")
                        await client.publish("mordomo.orchestrator.request", json.dumps({"text": cmd, "user_id": "debug"}).encode())
                    else:
                        logger.error("DEBUG_WS: NATS disconnected for legacy simulate")
            
            elif "bytes" in data and client.is_connected:
                logger.info(f"DEBUG_WS: Received {len(data['bytes'])} audio bytes. Publishing to stream.")
                await client.publish("mordomo.audio.stream", data["bytes"])
    except WebSocketDisconnect:
        logger.warning("DEBUG_WS: WebSocket DISCONNECTED")
        if sub: await sub.unsubscribe()
    except Exception as e:
        logger.error(f"DEBUG_WS: UNEXPECTED ERROR: {e}")
        if sub: await sub.unsubscribe()
    except Exception:
        if sub: await sub.unsubscribe()
