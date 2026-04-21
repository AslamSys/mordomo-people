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
    logger.info("DEBUG_WS: Connection accepted (STUB MODE)")
    try:
        while True:
            data = await websocket.receive_text()
            logger.info(f"DEBUG_WS: Received: {data}")
            # Echo back
            await websocket.send_text(f"Echo: {data}")
    except WebSocketDisconnect:
        logger.warning("DEBUG_WS: Disconnected")
    except Exception as e:
        logger.error(f"DEBUG_WS: Error: {e}")
