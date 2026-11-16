"""HW8–10 HTTP contract, with a CPU simulator, streaming and Kubernetes lifecycle."""
import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, ConfigDict, Field

MODE = "simulated"
POD = os.getenv("HOSTNAME", "local")
WARMUP = float(os.getenv("WARMUP_SECONDS", "20"))
CAPACITY = int(os.getenv("CONCURRENT", "1"))
QUEUE_LIMIT = int(os.getenv("QUEUE_LIMIT", "8"))
CACHE_LIMIT = int(os.getenv("CACHE_LIMIT", "12"))
HIT = float(os.getenv("HIT_SECONDS", "0.04"))
MISS = float(os.getenv("MISS_SECONDS", "0.28"))
BUCKETS = (.01,.025,.05,.075,.1,.15,.2,.3,.4,.5,.75,1,1.5,2,3,4,6,10,15,25,40)
TTFC = Histogram("lab_ttfc_seconds", "First emitted chunk of completed streams", buckets=BUCKETS)
TOTAL = Histogram("lab_total_seconds", "Wall time of completed streams", buckets=BUCKETS)
QUEUE = Histogram("lab_queue_seconds", "Wait before processing, completed streams", buckets=BUCKETS)
RUNNING = Gauge("lab_running", "Currently executing")
WAITING = Gauge("lab_waiting", "Currently waiting")
REQUESTS = Counter("lab_requests", "Request dispositions", ["outcome"])
for outcome in ("completed", "rejected", "unready", "aborted"):
    REQUESTS.labels(outcome)


def emit(event, **values):
    print(json.dumps(dict(event=event,t=time.time(),**{"pod":POD,"backend_mode":MODE,**values}), ensure_ascii=False), flush=True)


class SimulatedBackend:
    def __init__(self, model):
        self.model_id = model["model_id"]
        self.cache = OrderedDict()

    def clear(self):
        self.cache.clear()

    def cost(self, prefix):
        hit = prefix in self.cache
        self.cache[prefix] = True
        self.cache.move_to_end(prefix)
        while len(self.cache) > CACHE_LIMIT:
            self.cache.popitem(last=False)
        return (HIT if hit else MISS), hit


def burn(seconds):
    until = time.thread_time() + seconds
    block = b"lab"
    while time.thread_time() < until:
        for _ in range(512):
            block = hashlib.sha256(block).digest()


class Input(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    prompt: str = Field(min_length=1, max_length=4000)
    history: list[str] = Field(default_factory=list, max_length=12)
    prefix: str = Field(default="default", min_length=1, max_length=128)
    session_id: str = Field(default="", max_length=80)
    max_new_tokens: int = Field(default=32, ge=32, le=32)
    chunks: int = Field(default=4, ge=1, le=32)
    gap: float = Field(default=.10, ge=0, le=1)
    cpu_seconds: float = Field(default=0, ge=0, le=.6)
    stream: bool = True


@asynccontextmanager
async def lifespan(app):
    raw = Path(os.getenv("MODEL_PATH","/model/model.json")).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != os.environ["MODEL_SHA256"]:
        raise ValueError("Model artifact hash mismatch")
    backend = SimulatedBackend(json.loads(raw))
    app.state.backend = backend
    app.state.model_hash = digest
    app.state.warmed = False
    app.state.draining = False
    app.state.active = 0
    app.state.waiting = 0
    app.state.admitted = 0
    app.state.slots = asyncio.Semaphore(CAPACITY)

    async def warm():
        emit("warmup_start", model_id=backend.model_id, seconds=WARMUP)
        await asyncio.sleep(WARMUP)
        if os.getenv("NEVER_READY","false") != "true":
            app.state.warmed = True
            emit("warmup_end", model_id=backend.model_id)
        else:
            emit("warmup_failed", model_id=backend.model_id)
    task = asyncio.create_task(warm())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


app = FastAPI(lifespan=lifespan)


def ready():
    return app.state.warmed and not app.state.draining


def identity():
    return dict(pod=POD, model_id=app.state.backend.model_id, backend_mode=MODE,
                runtime_sha256=app.state.model_hash)


@app.get("/health")
@app.get("/live")
async def health():
    return {"alive": True, **identity()}


@app.get("/startup")
@app.get("/ready")
async def readiness():
    return JSONResponse({"ready":ready(), **identity()}, status_code=200 if ready() else 503)


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/internal/drain")
async def drain():
    app.state.draining = True
    emit("drain", model_id=identity()["model_id"], active=app.state.active, waiting=app.state.waiting)
    return {"draining": True}


@app.post("/internal/clear")
async def clear():
    if app.state.active or app.state.waiting:
        raise HTTPException(409, "busy")
    app.state.backend.clear()
    return identity()


@app.post("/generate")
async def generate(item: Input):
    start = time.monotonic()
    state = app.state
    ids = dict(request_id=uuid.uuid4().hex,session_id=item.session_id, **identity())
    def denied(status, reason):
        REQUESTS.labels(reason).inc()
        emit(reason,**ids, status=status)
        return JSONResponse({**ids,"error":reason}, status_code=status)
    if not ready():
        return denied(503,"unready")
    if not item.prompt.strip() or sum(map(len,item.history)) > 12000:
        return denied(422,"rejected")
    if state.admitted >= CAPACITY + QUEUE_LIMIT:
        return denied(429,"rejected")
    state.admitted += 1

    async def events():
        state.waiting += 1
        WAITING.inc()
        acquired = False
        completed = False
        first = None
        try:
            try:
                await state.slots.acquire()
                acquired = True
            finally:
                WAITING.dec()
                state.waiting -= 1
            state.active += 1
            RUNNING.inc()
            wait = time.monotonic() - start
            delay,hit = state.backend.cost(item.prefix)
            if item.cpu_seconds:
                await asyncio.to_thread(burn,item.cpu_seconds)
            await asyncio.sleep(delay)
            for i in range(item.chunks):
                if i:
                    await asyncio.sleep(item.gap)
                if first is None:
                    first = time.monotonic() - start
                yield dict(**ids, kind="chunk", chunk=i+1,
                           text=item.history[-1] if item.history else item.prompt,
                           queue_seconds=wait, cache_hit=hit)
            elapsed = time.monotonic() - start
            TTFC.observe(first)
            TOTAL.observe(elapsed)
            QUEUE.observe(wait)
            REQUESTS.labels("completed").inc()
            completed = True
            emit("completed",**ids,chunks=item.chunks,ttfc_seconds=first,total_seconds=elapsed,
                 queue_seconds=wait,cache_hit=hit)
            yield dict(**ids,kind="done",chunks=item.chunks,generation_seconds=elapsed-wait,
                       total_seconds=elapsed)
        finally:
            state.admitted -= 1
            if acquired:
                state.active -= 1
                RUNNING.dec()
                state.slots.release()
            if not completed:
                REQUESTS.labels("aborted").inc()
                emit("aborted",**ids)

    headers={"X-Pod": POD,"X-Model-ID":ids["model_id"],"X-Request-ID":ids["request_id"]}
    if item.stream:
        async def ndjson():
            async for packet in events():
                yield json.dumps(packet, ensure_ascii=False)+"\n"
        return StreamingResponse(ndjson(),headers=headers,media_type="application/x-ndjson")
    packets = [p async for p in events()]
    return JSONResponse({**packets[-1], "answer":packets[0]["text"], "output_tokens":None},headers=headers)
