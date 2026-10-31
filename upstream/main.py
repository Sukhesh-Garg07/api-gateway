"""Tiny demo upstream service the gateway proxies to.

Exists so the whole stack runs standalone. In a real deployment this is
whatever backend service sits behind the gateway.

Run:  uvicorn upstream.main:app --port 9000
"""

from __future__ import annotations

import asyncio
import time

from fastapi import FastAPI, Request

app = FastAPI(title="demo upstream", version="1.0.0")


@app.get("/")
async def root():
    return {"service": "demo-upstream", "status": "ok"}


@app.api_route("/echo", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def echo(request: Request):
    body = await request.body()
    return {
        "method": request.method,
        "path": request.url.path,
        "query": dict(request.query_params),
        "api_key_id": request.headers.get("x-api-key-id"),
        "body": body.decode("utf-8", errors="replace"),
    }


@app.get("/search")
async def search(q: str = ""):
    return {
        "query": q,
        "results": [
            {"id": i, "title": f"result {i} for '{q}'"} for i in range(1, 4)
        ],
    }


@app.get("/items/{item_id}")
async def item(item_id: int):
    return {"id": item_id, "name": f"item-{item_id}", "price": item_id * 10}


@app.get("/slow")
async def slow(ms: int = 100):
    started = time.perf_counter()
    await asyncio.sleep(ms / 1000)
    return {"slept_ms": ms, "actual_ms": round((time.perf_counter() - started) * 1000, 1)}
