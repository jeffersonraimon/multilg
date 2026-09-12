import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .connectors import discover_hyperglass, execute_one, validate_target
from .database import Database
from .schemas import BatchResult, HyperglassDiscoveryInput, LookingGlassInput, QueryInput

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


@app.get("/api/looking-glasses/export")
def export_looking_glasses():
    items = database().list(reveal=True)
    export_data = [
        {
            "name": item["name"],
            "protocol": item["protocol"],
            "enabled": item["enabled"],
            "config": item["config"],
        }
        for item in items
    ]
    headers = {"Content-Disposition": "attachment; filename=multilg-looking-glasses.json"}
    return Response(
        content=json.dumps(export_data, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers=headers,
    )


@app.post("/api/looking-glasses/import", status_code=201)
async def import_looking_glasses(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Arquivo JSON inválido")

    raw_items = body.get("items") if isinstance(body, dict) else body
    if not isinstance(raw_items, list):
        raise HTTPException(
            400, "O JSON deve ser uma lista de LGs ou um objeto com a propriedade 'items'"
        )

    if not raw_items:
        raise HTTPException(400, "Nenhum Looking Glass para importar no arquivo")

    validated_items: list[LookingGlassInput] = []
    for idx, item in enumerate(raw_items, 1):
        if not isinstance(item, dict):
            raise HTTPException(422, f"Elemento #{idx} inválido (deve ser um objeto)")
        try:
            validated_items.append(LookingGlassInput.model_validate(item))
        except Exception as exc:
            lg_name = item.get("name", "sem nome")
            raise HTTPException(
                422, f"Erro de validação no LG #{idx} ('{lg_name}'): {exc}"
            ) from exc

    created_count = 0
    updated_count = 0
    result_items = []

    for raw_item, validated_item in zip(raw_items, validated_items):
        item_data = validated_item.model_dump(mode="json")
        existing = None
        if isinstance(raw_item, dict) and raw_item.get("id"):
            existing = database().get(str(raw_item["id"]))
        if not existing:
            existing = database().get_by_name(validated_item.name)

        if existing:
            updated = database().update(existing["id"], item_data)
            if updated:
                result_items.append(updated)
                updated_count += 1
        else:
            created = database().create(item_data)
            result_items.append(created)
            created_count += 1

    return {
        "imported": len(result_items),
        "created": created_count,
        "updated": updated_count,
        "items": result_items,
    }


@app.post("/api/looking-glasses", status_code=201)
def create_looking_glass(payload: LookingGlassInput):
    return database().create(payload.model_dump(mode="json"))


@app.put("/api/looking-glasses/{item_id}")
def update_looking_glass(item_id: str, payload: LookingGlassInput):
    result = database().update(item_id, payload.model_dump(mode="json"))
    if not result:
        raise HTTPException(404, "Looking Glass não encontrado")
    return result


@app.post("/api/looking-glasses/{item_id}/duplicate", status_code=201)
def duplicate_looking_glass(item_id: str):
    result = database().duplicate(item_id)
    if not result:
        raise HTTPException(404, "Looking Glass não encontrado")
    return result


@app.delete("/api/looking-glasses/{item_id}", status_code=204)
def delete_looking_glass(item_id: str):
    if not database().delete(item_id):
        raise HTTPException(404, "Looking Glass não encontrado")
    return Response(status_code=204)


@app.post("/api/hyperglass/discover")
async def hyperglass_discovery(payload: HyperglassDiscoveryInput):
    try:
        return await discover_hyperglass(payload.model_dump(mode="json"))
    except (ConnectionError, TimeoutError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


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

    async def events():
        started = time.perf_counter()
        queue: asyncio.Queue[dict] = asyncio.Queue()

        async def run_item(item):
            last_update = 0.0

            async def report_partial(output: str):
                nonlocal last_update
                now = asyncio.get_running_loop().time()
                if now - last_update < 0.15:
                    return
                last_update = now
                await queue.put(
                    {
                        "type": "partial",
                        "looking_glass_id": item["id"],
                        "output": output,
                    }
                )

            async with semaphore:
                result = await execute_one(
                    item,
                    payload.operation,
                    target,
                    report_partial if payload.operation.value == "traceroute" else None,
                )
            await queue.put(
                {"type": "result", "result": result.model_dump(mode="json")}
            )

        tasks = [asyncio.create_task(run_item(item)) for item in items]
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
            completed = 0
            while completed < len(tasks):
                event = await queue.get()
                if event["type"] == "result":
                    completed += 1
                yield json.dumps(event, ensure_ascii=False) + "\n"
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
