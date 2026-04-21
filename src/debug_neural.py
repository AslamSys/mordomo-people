"""
Debug Neural Pipeline — WebSocket bridge + pipeline monitor.

Audio injection path (canonical):
  Browser mic → WebSocket bytes → 30 ms chunks → ZMQ PUB (audio.raw)
      → [VAD]  ← already subscribed
      → [Wake Word detector] ← already subscribed
      → [Whisper ASR] ← already subscribed

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

# ZMQ constants
ZMQ_VAD_URL = os.getenv("ZMQ_VAD_URL", "tcp://audio-capture-vad:5555")
ZMQ_TOPIC   = os.getenv("ZMQ_TOPIC", "audio.raw").encode()

# PCM frame constants (must match VAD config)
# 16 kHz · 16-bit · mono · 30 ms = 960 bytes per frame
SAMPLE_RATE    = 16_000
FRAME_DURATION = 30                          # ms
FRAME_BYTES    = SAMPLE_RATE * FRAME_DURATION // 1000 * 2   # 960


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

    # ── ZeroMQ PUB socket — injects frames directly into the VAD bus ──────────
    zmq_ctx  = azmq.Context.instance()
    zmq_sock = zmq_ctx.socket(zmq.PUB)
    zmq_ok   = False
    try:
        zmq_sock.connect(ZMQ_VAD_URL)
        # ZMQ slow-joiner: give subscribers a moment before first publish
        await asyncio.sleep(0.05)
        zmq_ok = True
        logger.info(f"DEBUG_WS: ZMQ PUB connected → {ZMQ_VAD_URL}")
    except Exception as e:
        logger.error(f"DEBUG_WS: ZMQ connect failed: {e}")

    _pcm_buf = bytearray()

    async def _inject(raw: bytes):
        """Buffer raw PCM, flush in strict 30 ms frames via ZMQ multipart."""
        if not zmq_ok:
            return
        _pcm_buf.extend(raw)
        while len(_pcm_buf) >= FRAME_BYTES:
            frame = bytes(_pcm_buf[:FRAME_BYTES])
            del _pcm_buf[:FRAME_BYTES]
            try:
                # Multipart: [topic_bytes, pcm_bytes] — matches publisher.py format
                await zmq_sock.send_multipart([ZMQ_TOPIC, frame])
            except Exception as e:
                logger.warning(f"DEBUG_WS: ZMQ send error: {e}")

    # ── NATS telemetry subscription ───────────────────────────────────────────
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

    # ── Main receive loop ─────────────────────────────────────────────────────
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
                # ── CANONICAL PATH: PC mic → ZMQ VAD bus ─────────────────
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
