import json
import logging
import asyncio
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger("mordomo-debug")
app = FastAPI(title="Neural Pipeline Debugger")
templates = Jinja2Templates(directory="src/templates")

@app.get("/monitor", response_class=HTMLResponse)
async def monitor_page(request: Request):
    return templates.TemplateResponse(request=request, name="debug_audio.html", context={})

@app.websocket("/ws")
async def monitor_ws(websocket: WebSocket):
    await websocket.accept()
    logger.info("DEBUG_WS: Connection accepted (Production Mode)")
    
    sub = None
    if client.is_connected:
        try:
            sub = await client.subscribe("mordomo.>", cb=msg_handler)
            logger.info("DEBUG_WS: Subscribed back to NATS (Full Monitor)")
        except Exception as e:
            logger.error(f"DEBUG_WS: NATS Sub error: {e}")
    
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
