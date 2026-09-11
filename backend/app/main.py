import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .connectors import execute_one, validate_target
from .database import Database
from .schemas import BatchResult, LookingGlassInput, QueryInput

db: Database | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global db
    db = Database()
    yield


app = FastAPI(title="MultiLG", version="1.0.0", lifespan=lifespan)


def database() -> Database:
    if db is None:
        raise RuntimeError("Banco ainda não inicializado")
    return db


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/looking-glasses")
def list_looking_glasses():
    return database().list()


@app.post("/api/looking-glasses", status_code=201)
def create_looking_glass(payload: LookingGlassInput):
    return database().create(payload.model_dump(mode="json"))


@app.put("/api/looking-glasses/{item_id}")
def update_looking_glass(item_id: str, payload: LookingGlassInput):
    result = database().update(item_id, payload.model_dump(mode="json"))
    if not result:
        raise HTTPException(404, "Looking Glass não encontrado")
    return result


@app.delete("/api/looking-glasses/{item_id}", status_code=204)
def delete_looking_glass(item_id: str):
    if not database().delete(item_id):
        raise HTTPException(404, "Looking Glass não encontrado")
    return Response(status_code=204)


@app.post("/api/query", response_model=BatchResult)
async def run_query(payload: QueryInput):
    try:
        target = validate_target(payload.target, payload.operation)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    items = [item for item in database().list(reveal=True) if item["enabled"]]
    if payload.looking_glass_ids is not None:
        selected = set(payload.looking_glass_ids)
        items = [item for item in items if item["id"] in selected]
    if not items:
        raise HTTPException(400, "Nenhum Looking Glass habilitado foi selecionado")
    concurrency = max(1, int(os.getenv("LG_QUERY_CONCURRENCY", "10")))
    semaphore = asyncio.Semaphore(concurrency)

    async def limited(item):
        async with semaphore:
            return await execute_one(item, payload.operation, target)

    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    results = await asyncio.gather(*(limited(item) for item in items))
    return BatchResult(
        target=target,
        operation=payload.operation,
        started_at=started_at,
        duration_ms=round((time.perf_counter() - started) * 1000),
        results=results,
    )


@app.post("/api/query/stream")
async def stream_query(payload: QueryInput):
    try:
        target = validate_target(payload.target, payload.operation)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    items = [item for item in database().list(reveal=True) if item["enabled"]]
    if payload.looking_glass_ids is not None:
        selected = set(payload.looking_glass_ids)
        items = [item for item in items if item["id"] in selected]
    if not items:
        raise HTTPException(400, "Nenhum Looking Glass habilitado foi selecionado")
    concurrency = max(1, int(os.getenv("LG_QUERY_CONCURRENCY", "10")))
    semaphore = asyncio.Semaphore(concurrency)

    async def limited(item):
        async with semaphore:
            return await execute_one(item, payload.operation, target)

    async def events():
        started = time.perf_counter()
        tasks = [asyncio.create_task(limited(item)) for item in items]
        try:
            yield json.dumps(
                {
                    "type": "start",
                    "target": target,
                    "operation": payload.operation.value,
                    "looking_glass_ids": [item["id"] for item in items],
                    "total": len(items),
                }
            ) + "\n"
            for completed in asyncio.as_completed(tasks):
                result = await completed
                yield json.dumps(
                    {"type": "result", "result": result.model_dump(mode="json")},
                    ensure_ascii=False,
                ) + "\n"
            yield json.dumps(
                {
                    "type": "complete",
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                }
            ) + "\n"
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        candidate = STATIC_DIR / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")
