"""
Debug Neural Pipeline — WebSocket bridge + pipeline monitor.

Audio injection path (canonical):
  Browser mic → WebSocket bytes → 30 ms chunks → ZMQ PUSH (tcp://audio-capture-vad:5556)
      → [VAD PULL] applies VAD+AGC → republishes via ZMQ PUB (5555)
      → [Wake Word] ← subscribed to 5555
      → [Whisper ASR] ← subscribed to 5555

NATS is used only for control commands and telemetry.
"""
import json
import logging
import asyncio
import os

import zmq
import zmq.asyncio as azmq
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from nats.aio.client import Client as NATS

logger = logging.getLogger("mordomo-debug")
app = FastAPI(title="Neural Pipeline Debugger")
templates = Jinja2Templates(directory="src/templates")

# Shared NATS client
_nc = NATS()

# ZMQ constants — PUSH to VAD's PULL socket (port 5556)
ZMQ_VAD_URL = os.getenv("ZMQ_VAD_URL", "tcp://mordomo-audio-capture-vad:5556")

# PCM frame constants (must match VAD config)
# 16 kHz · 16-bit · mono · 30 ms = 960 bytes per frame
SAMPLE_RATE    = 16_000
FRAME_DURATION = 30                                       # ms
FRAME_BYTES    = SAMPLE_RATE * FRAME_DURATION // 1000 * 2  # 960 bytes


async def get_nc() -> NATS:
    if not _nc.is_connected:
        try:
            await _nc.connect("nats://nats:4222", pending_size=1024 * 1024 * 10)
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
    logger.info("DEBUG_WS: Connection accepted")

    client = await get_nc()
    sub = None

    # ── ZeroMQ PUSH socket — sends frames to VAD's PULL socket ────────────
    zmq_ctx  = azmq.Context.instance()
    zmq_sock = zmq_ctx.socket(zmq.PUSH)
    zmq_ok   = False
    try:
        zmq_sock.connect(ZMQ_VAD_URL)
        # Give ZMQ time to establish the connection
        await asyncio.sleep(0.1)
        zmq_ok = True
        logger.info(f"DEBUG_WS: ZMQ PUSH connected → {ZMQ_VAD_URL}")
    except Exception as e:
        logger.error(f"DEBUG_WS: ZMQ PUSH connect failed: {e}")

    _pcm_buf = bytearray()

    async def _inject(raw: bytes):
        """Buffer raw PCM, flush in strict 30 ms frames via ZMQ PUSH."""
        if not zmq_ok:
            return
        _pcm_buf.extend(raw)
        pushed = 0
        while len(_pcm_buf) >= FRAME_BYTES:
            frame = bytes(_pcm_buf[:FRAME_BYTES])
            del _pcm_buf[:FRAME_BYTES]
            try:
                await zmq_sock.send(frame)
                pushed += 1
            except Exception as e:
                logger.warning(f"DEBUG_WS: ZMQ PUSH send error: {e}")
        if pushed:
            logger.debug(f"DEBUG_WS: Pushed {pushed} frames to VAD")

    # ── NATS telemetry subscription ───────────────────────────────────────
    async def _nats_handler(msg):
        try:
            subject = msg.subject
            data    = msg.data

            if subject.endswith(".energy"):
                content = json.loads(data.decode())
            elif subject.endswith(".speech") or subject.endswith(".stream"):
                content = {"type": "audio_event", "len": len(data)}
            else:
                try:
                    content = json.loads(data.decode())
                except Exception:
                    content = data.decode(errors="replace")

            await websocket.send_json({"topic": subject, "payload": content})
        except Exception:
            pass

    if client.is_connected:
        try:
            sub = await client.subscribe("mordomo.>", cb=_nats_handler)
            logger.info("DEBUG_WS: Subscribed to NATS telemetry")
        except Exception as e:
            logger.error(f"DEBUG_WS: NATS subscription failed: {e}")

    # ── Main receive loop ─────────────────────────────────────────────────
    try:
        while True:
            data = await websocket.receive()

            if "text" in data:
                try:
                    msg     = json.loads(data["text"])
                    topic   = msg.get("topic")
                    payload = msg.get("payload", {})
                    if topic and client.is_connected:
                        await client.publish(topic, json.dumps(payload).encode())
                        await client.flush()
                except Exception:
                    text = data["text"]
                    if text.startswith("simulate:") and client.is_connected:
                        cmd = text.replace("simulate:", "")
                        await client.publish(
                            "mordomo.orchestrator.request",
                            json.dumps({"text": cmd, "user_id": "debug"}).encode(),
                        )

            elif "bytes" in data:
                # ── CANONICAL PATH: PC mic → ZMQ PUSH → VAD PULL → PUB → consumers ──
                await _inject(data["bytes"])

    except WebSocketDisconnect:
        logger.warning("DEBUG_WS: Client disconnected")
    except Exception as e:
        logger.error(f"DEBUG_WS: Unexpected error: {e}")
    finally:
        if sub:
            await sub.unsubscribe()
        zmq_sock.close()
        logger.info("DEBUG_WS: Cleanup done")
