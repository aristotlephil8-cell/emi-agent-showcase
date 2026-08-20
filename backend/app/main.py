from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from . import __version__
from .config import Settings
from .contracts import DecisionRequest, RunRequest
from .data import CaseNotFoundError
from .providers import ProviderError
from .runtime import (
    GraphManager,
    InvalidRunStateError,
    RunNotFoundError,
    open_graph_manager,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with open_graph_manager(resolved_settings) as manager:
            app.state.graph_manager = manager
            yield

    app = FastAPI(
        title="EMI-Agent API",
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    def manager(request: Request) -> GraphManager:
        return request.app.state.graph_manager

    @app.get("/api/v1/health")
    async def health(request: Request) -> dict[str, Any]:
        graph_manager = manager(request)
        return {
            "status": "ok",
            "version": __version__,
            "checkpoint": "sqlite-demo-only",
            "strict_msgpack": True,
            "live_provider_available": graph_manager.settings.live_provider_available,
        }

    @app.get("/api/v1/cases")
    async def list_cases(request: Request) -> list[dict[str, Any]]:
        return manager(request).catalog.list_public()

    @app.post("/api/v1/runs/stream")
    async def start_run(payload: RunRequest, request: Request) -> EventSourceResponse:
        try:
            session = await manager(request).prepare_run(payload)
        except CaseNotFoundError as exc:
            raise HTTPException(status_code=404, detail="case_not_found") from exc
        except ProviderError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return _event_response(manager(request).stream(session))

    @app.post("/api/v1/runs/{run_id}/resume/stream")
    async def resume_run(run_id: str, request: Request) -> EventSourceResponse:
        try:
            session = await manager(request).prepare_resume(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run_not_found") from exc
        except InvalidRunStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ProviderError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return _event_response(manager(request).stream(session))

    @app.get("/api/v1/runs/{run_id}")
    async def get_run(run_id: str, request: Request) -> dict[str, Any]:
        try:
            return await manager(request).get_run(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run_not_found") from exc

    @app.post("/api/v1/runs/{run_id}/decision")
    async def decide(
        run_id: str, payload: DecisionRequest, request: Request
    ) -> dict[str, Any]:
        try:
            return await manager(request).decide(run_id, payload)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run_not_found") from exc
        except InvalidRunStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/v1/evaluation/summary")
    async def evaluation_summary(request: Request) -> dict[str, Any]:
        return manager(request).evaluation_summary()

    return app


def _event_response(events: AsyncIterator[dict[str, Any]]) -> EventSourceResponse:
    async def encode() -> AsyncIterator[dict[str, str]]:
        async for event in events:
            yield {
                "id": event["event_id"],
                "event": event["type"],
                "data": json.dumps(event, ensure_ascii=False, separators=(",", ":")),
            }

    return EventSourceResponse(
        encode(),
        ping=15,
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


app = create_app()

