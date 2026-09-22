import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from poller import LabPoller


STATIC_DIR = Path(__file__).parent / "static"
TOPOLOGY_PATH = Path(os.environ.get("LAB_TOPOLOGY", "/lab/topology.yml"))
LAB_PREFIX = os.environ.get("LAB_PREFIX", "clab-simple-lab")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "2"))
# How many events are kept for a page that arrives late. 500 at this fabric's
# event rate is hours of a quiet lab and still covers a full `clear ip bgp *`,
# which produces a few dozen.
EVENTS_RING = int(os.environ.get("EVENTS_RING", "500"))

clients: set[WebSocket] = set()
poller: LabPoller | None = None


async def broadcast(message: dict):
    if not clients:
        return
    payload = json.dumps(message)
    dead = []
    for ws in list(clients):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global poller
    poller = LabPoller(
        topology_path=TOPOLOGY_PATH,
        lab_prefix=LAB_PREFIX,
        broadcast=broadcast,
        interval=POLL_INTERVAL,
        events_ring=EVENTS_RING,
    )
    task = asyncio.create_task(poller.run())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
async def state():
    if poller is None:
        return {"ready": False}
    return {"ready": True, "data": poller.last_state, "nodes": poller.nodes}


@app.get("/api/events")
async def events(since: int = 0):
    """What has happened, for a page that was not connected when it did.

    `since` is the last id the caller already has, so a reconnecting page asks
    only for the gap. It is served from the same ring the socket broadcasts
    from, with the same ids, so an event delivered twice is recognisable as
    one event rather than rendered twice.
    """
    if poller is None:
        return {"ready": False, "events": [], "lastId": 0}
    return {
        "ready": True,
        "epoch": poller.epoch,
        "events": poller.events_since(since),
        "lastId": poller.last_event_id,
    }


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    clients.add(websocket)
    if poller is not None:
        await websocket.send_text(json.dumps({
            "type": "snapshot",
            "nodes": poller.nodes,
            "data": poller.last_state,
        }))
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(websocket)
