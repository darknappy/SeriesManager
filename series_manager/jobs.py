"""Ejecución de trabajos en segundo plano con progreso persistente."""
from __future__ import annotations

import json
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .config import Config
from .db import init_jobs_db, session
from .utils import json_dumps, now_iso

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="seriesadmin")
_futures: dict[str, Any] = {}
_futures_lock = threading.Lock()
_init_lock = threading.Lock()
_initialized: set[str] = set()


JOB_LABELS = {
    "scan": "Escaneo de la biblioteca",
    "sync": "Sincronización con TVmaze",
    "match": "Búsqueda de emparejamientos",
    "match_one": "Búsqueda de candidatos",
    "plan": "Generación del plan",
    "apply": "Aplicación del plan",
    "undo": "Deshacer el plan",
    "watch": "Actualización de la lista de seguimiento",
    "jellyfin_backup": "Copia del estado de Jellyfin",
}


class JobError(RuntimeError):
    pass


def _db(cfg: Config):
    """Sesión contra jobs.db (separada de catalog.db para no bloquear escaneos)."""
    path = str(cfg.jobs_db_path)
    if path not in _initialized:
        with _init_lock:
            if path not in _initialized:
                init_jobs_db(path)
                _initialized.add(path)
    return session(path)


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _create_job(cfg: Config, kind: str, params: dict[str, Any] | None) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _db(cfg) as con:
        con.execute(
            "INSERT INTO jobs(id, kind, status, progress, message, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (job_id, kind, "queued", 0, "En cola", now_iso()),
        )
    return job_id


def _update_job(cfg: Config, job_id: str, **values: Any) -> None:
    if not values:
        return
    with _db(cfg) as con:
        assignments = ", ".join(f"{key}=?" for key in values)
        con.execute(
            f"UPDATE jobs SET {assignments} WHERE id=?",
            (*values.values(), job_id),
        )


def _progress_writer(
    cfg: Config,
    job_id: str,
    external: Callable[[int, str], None] | None = None,
) -> Callable[[int, str], None]:
    last = {"pct": -1, "message": "", "time": 0.0}

    def progress(pct: int, message: str) -> None:
        import time

        pct = max(0, min(100, int(pct)))
        moment = time.monotonic()
        if (
            pct == last["pct"]
            and message == last["message"]
            and moment - last["time"] < 1.0
        ):
            if external:
                external(pct, message)
            return
        last.update({"pct": pct, "message": message, "time": moment})
        try:
            _update_job(cfg, job_id, progress=pct, message=message, status="running")
        except Exception:
            pass
        if external:
            external(pct, message)

    return progress


def _perform(cfg: Config, kind: str, params: dict[str, Any], progress: Callable[[int, str], None]) -> Any:
    """Despacha un tipo de trabajo a su implementación real."""
    # Importaciones tardías para evitar ciclos y acelerar el arranque.
    if kind == "scan":
        from .scan import scan_library

        return scan_library(
            cfg,
            progress,
            fingerprint=bool(params.get("fingerprint", True)),
            auto_match=bool(params.get("auto_match", True)),
        )
    if kind == "sync":
        from .sync import sync_metadata

        return sync_metadata(
            cfg,
            progress,
            force=bool(params.get("force", False)),
            auto_match=bool(params.get("auto_match", True)),
        )
    if kind == "match":
        from .matching import refresh_all

        return refresh_all(cfg, progress)
    if kind == "match_one":
        from .matching import search_candidates, store_candidates
        from .db import session

        series_id = int(params["series_id"])
        with session(cfg.db_path) as con:
            series = con.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
            if not series:
                raise JobError(f"No existe la serie {series_id}")
            progress(15, f"Buscando candidatos para {series['title']}")
            candidates = search_candidates(cfg, series)
            store_candidates(con, series_id, candidates)
        progress(100, f"{len(candidates)} candidatos encontrados")
        return {"series_id": series_id, "candidates": len(candidates)}
    if kind == "plan":
        from .plan import build_plan

        return build_plan(cfg, str(params.get("mode") or "full"), progress)
    if kind == "apply":
        from .apply import apply_plan

        return apply_plan(cfg, int(params["plan_id"]), progress)
    if kind == "undo":
        from .apply import undo_plan

        return undo_plan(cfg, int(params["plan_id"]), progress)
    if kind == "watch":
        from .airing import sync_watchlist

        return sync_watchlist(cfg, progress)
    if kind == "jellyfin_backup":
        from .jellyfin import backup_watch_state

        return backup_watch_state(cfg)
    raise JobError(f"Tipo de trabajo desconocido: {kind}")


def _run(cfg: Config, job_id: str, kind: str, params: dict[str, Any]) -> dict[str, Any]:
    _update_job(
        cfg, job_id, status="running", started_at=now_iso(), message="Iniciando…"
    )
    progress = _progress_writer(cfg, job_id)
    try:
        result = _perform(cfg, kind, params, progress)
        payload = _jsonable(result)
        _update_job(
            cfg,
            job_id,
            status="done",
            progress=100,
            message="Terminado",
            result=json_dumps(payload),
            finished_at=now_iso(),
        )
        return {"ok": True, "job_id": job_id, "result": payload}
    except Exception as exc:
        _update_job(
            cfg,
            job_id,
            status="error",
            message=str(exc),
            error=f"{exc}\n{traceback.format_exc(limit=6)}",
            finished_at=now_iso(),
        )
        return {"ok": False, "job_id": job_id, "error": str(exc)}


def submit(
    cfg: Config,
    kind: str,
    params: dict[str, Any] | None = None,
) -> str:
    """Encola un trabajo en segundo plano y devuelve su id."""
    params = params or {}
    job_id = _create_job(cfg, kind, params)
    future = _executor.submit(_run, cfg, job_id, kind, params)
    with _futures_lock:
        _futures[job_id] = future
    return job_id


def run(
    cfg: Config,
    kind: str,
    params: dict[str, Any] | None = None,
    progress: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    """Ejecuta un trabajo de forma síncrona (para la CLI)."""
    params = params or {}
    job_id = _create_job(cfg, kind, params)
    _update_job(cfg, job_id, status="running", started_at=now_iso(), message="Iniciando…")
    writer = _progress_writer(cfg, job_id, progress)
    try:
        result = _perform(cfg, kind, params, writer)
        payload = _jsonable(result)
        _update_job(
            cfg,
            job_id,
            status="done",
            progress=100,
            message="Terminado",
            result=json_dumps(payload),
            finished_at=now_iso(),
        )
        return {"ok": True, "job_id": job_id, "result": payload}
    except Exception as exc:
        _update_job(
            cfg,
            job_id,
            status="error",
            message=str(exc),
            error=f"{exc}\n{traceback.format_exc(limit=6)}",
            finished_at=now_iso(),
        )
        return {"ok": False, "job_id": job_id, "error": str(exc)}


def get(cfg: Config, job_id: str) -> dict[str, Any] | None:
    with _db(cfg) as con:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
    item["label"] = JOB_LABELS.get(item["kind"], item["kind"])
    item["result_data"] = json.loads(item["result"]) if item.get("result") else None
    return item


def latest(cfg: Config, limit: int = 20) -> list[dict[str, Any]]:
    with _db(cfg) as con:
        rows = con.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["label"] = JOB_LABELS.get(item["kind"], item["kind"])
        result.append(item)
    return result


def active(cfg: Config) -> list[dict[str, Any]]:
    with _db(cfg) as con:
        rows = con.execute(
            "SELECT * FROM jobs WHERE status IN ('queued','running') "
            "ORDER BY created_at DESC"
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["label"] = JOB_LABELS.get(item["kind"], item["kind"])
        result.append(item)
    return result


def wait(cfg: Config, job_id: str, timeout: float | None = None) -> dict[str, Any] | None:
    with _futures_lock:
        future = _futures.get(job_id)
    if future:
        try:
            future.result(timeout=timeout)
        except Exception:
            pass
    return get(cfg, job_id)
