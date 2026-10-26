"""Panel web local (FastAPI) del gestor de series."""
from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.templating import Jinja2Templates

from . import jobs
from .airing import (
    WATCH_STATUSES,
    add_manual_watch,
    add_watch,
    advance_watch,
    annotate_season_downloads,
    discover_season,
    follow_anilist_ids,
    library_airing,
    list_watch,
    remove_watch,
    season_info,
    staging_downloads,
    update_watch,
    upcoming,
)
from .catalog import (
    attach_folder_alias,
    folder_choices,
    identify_folder,
    list_unidentified,
)
from .config import Config
from .db import init_db, session
from .jellyfin import JellyfinClient, JellyfinError, backup_watch_state
from .missing import actionable_episodes, actionable_summary
from .naming import parse_folder_info
from .providers.anilist import AniListClient, SEASON_ES
from .providers.tvmaze import TVmazeClient
from .utils import (
    countdown_es,
    format_date_es,
    format_datetime_es,
    human_size,
    json_loads,
    now_iso,
)

cfg = Config()
cfg.ensure_dirs()
init_db(cfg.db_path)

env = Environment(
    loader=FileSystemLoader(str(cfg.app_root / "templates")),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
templates = Jinja2Templates(env=env)
templates.env.filters["human_size"] = human_size
templates.env.filters["date_es"] = format_date_es
templates.env.filters["datetime_es"] = format_datetime_es
templates.env.filters["countdown"] = countdown_es
templates.env.filters["json_loads"] = json_loads

STATUS_ES = {
    "running": "En curso",
    "queued": "En cola",
    "done": "Terminado",
    "error": "Error",
    "ready": "Listo",
    "stale": "Obsoleto",
    "draft": "Borrador",
    "building": "Generando",
    "applying": "Aplicando",
    "applied": "Aplicado",
    "partial": "Parcial",
    "undone": "Deshecho",
    "matched": "Emparejada",
    "manual": "Manual",
    "seeded": "Semilla",
    "unmatched": "Sin emparejar",
    "present": "Presente",
    "missing": "Falta",
    "unaired": "Sin emitir",
    "quarantined": "En cuarentena",
    "pending": "Pendiente",
    "done": "Hecho",
    "skipped": "Omitido",
    "unknown": "Desconocido",
    "ended": "Finalizada",
    "canceled": "Cancelada",
    "airing": "En emisión",
    "library": "Biblioteca",
    "airing-root": "En emisión",
    "info": "Info",
    "warning": "Aviso",
    "critical": "Crítico",
}
ACTION_ES = {
    "move": "Mover",
    "rename": "Renombrar",
    "quarantine": "Cuarentena",
    "conflict": "Conflicto",
    "keep": "Conservar",
}
templates.env.filters["status_es"] = lambda value: STATUS_ES.get(str(value), value or "—")
templates.env.filters["action_es"] = lambda value: ACTION_ES.get(str(value), value or "—")
templates.env.globals["app_name"] = "SeriesAdmin"
templates.env.globals["season_es"] = SEASON_ES

app = FastAPI(title="SeriesAdmin", docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=str(cfg.app_root / "static")), name="static")
app.mount("/reports", StaticFiles(directory=str(cfg.reports_root), html=True), name="reports")


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def render(request: Request, template: str, **context: Any) -> HTMLResponse:
    context.setdefault("active", "")
    context.setdefault("message", request.query_params.get("msg"))
    context.setdefault("error", request.query_params.get("error"))
    context.setdefault("cfg", cfg)
    context.setdefault("today", dt.date.today().isoformat())
    context.setdefault("active_jobs", jobs.active(cfg))
    return templates.TemplateResponse(request, template, context)


def redirect(path: str, msg: str | None = None, error: str | None = None, **params: Any) -> RedirectResponse:
    query = {key: value for key, value in params.items() if value not in (None, "")}
    if msg:
        query["msg"] = msg
    if error:
        query["error"] = error
    url = path
    if query:
        url += "?" + urlencode(query)
    return RedirectResponse(url, status_code=303)


def fetch_series(con, series_id: int):
    row = con.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Serie no encontrada")
    return row


def start_job_redirect(kind: str, params: dict[str, Any] | None = None) -> RedirectResponse:
    job_id = jobs.submit(cfg, kind, params)
    return redirect(f"/jobs/{job_id}", msg="Trabajo iniciado.")


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    today = dt.date.today().isoformat()
    with session(cfg.db_path) as con:
        def scalar(sql: str, params: tuple = ()) -> int:
            return int(con.execute(sql, params).fetchone()[0] or 0)

        stats = {
            "series": scalar("SELECT COUNT(*) FROM series"),
            "episodes": scalar("SELECT COUNT(*) FROM episodes"),
            "present": scalar("SELECT COUNT(*) FROM episodes WHERE status='present'"),
            "missing": scalar("SELECT COUNT(*) FROM episodes WHERE status='missing'"),
            "missing_gaps": actionable_summary(con)["missing"],
            "unaired": scalar("SELECT COUNT(*) FROM episodes WHERE status='unaired'"),
            "videos": scalar(
                "SELECT COUNT(*) FROM files WHERE kind='video' AND status='present'"
            ),
            "issues": scalar("SELECT COUNT(*) FROM issues WHERE resolved=0"),
            "unmatched": scalar(
                "SELECT COUNT(*) FROM series WHERE match_status IN ('unmatched','seeded')"
            ),
            "airing": scalar("SELECT COUNT(*) FROM series WHERE is_airing=1"),
            "watch": scalar("SELECT COUNT(*) FROM watchlist"),
            "bytes": scalar(
                "SELECT COALESCE(SUM(size),0) FROM files WHERE kind='video' AND status='present'"
            ),
        }
        latest_plan = con.execute("SELECT * FROM plans ORDER BY id DESC LIMIT 1").fetchone()
        issues = con.execute(
            "SELECT i.*, s.title AS series_title FROM issues i "
            "LEFT JOIN series s ON s.id=i.series_id "
            "WHERE i.resolved=0 ORDER BY CASE i.severity WHEN 'critical' THEN 0 "
            "WHEN 'error' THEN 1 WHEN 'warning' THEN 2 ELSE 3 END, i.id DESC LIMIT 8"
        ).fetchall()
        last_scan = con.execute("SELECT value FROM meta WHERE key='last_scan'").fetchone()
        last_sync = con.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone()
        upcoming_library = con.execute(
            "SELECT id, title, next_episode, next_air_at, last_aired, cover_url "
            "FROM series WHERE is_airing=1 "
            "ORDER BY CASE WHEN next_air_at IS NULL THEN 1 ELSE 0 END, next_air_at LIMIT 6"
        ).fetchall()
    plan = dict(latest_plan) if latest_plan else None
    if plan:
        plan["summary_data"] = json_loads(plan.get("summary"), {}) or {}
    disk = None
    try:
        if cfg.library_root.exists():
            usage = shutil.disk_usage(cfg.library_root)
            disk = {
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "pct": round(usage.used / usage.total * 100, 1) if usage.total else 0,
            }
    except OSError:
        pass
    try:
        upcoming_items = upcoming(cfg, 21)
    except Exception:
        upcoming_items = []
    return render(
        request,
        "dashboard.html",
        active="dashboard",
        stats=stats,
        plan=plan,
        issues=[dict(row) for row in issues],
        upcoming_library=[dict(row) for row in upcoming_library],
        upcoming=upcoming_items,
        disk=disk,
        last_scan=last_scan["value"] if last_scan else None,
        last_sync=last_sync["value"] if last_sync else None,
        recent_jobs=jobs.latest(cfg, 6),
    )


# ---------------------------------------------------------------------------
# Series
# ---------------------------------------------------------------------------
@app.get("/series", response_class=HTMLResponse)
def series_list(
    request: Request,
    q: str = "",
    root: str = "",
    match: str = "",
    airing: str = "",
):
    where = ["1=1"]
    params: list[Any] = []
    if q.strip():
        where.append("(s.title LIKE ? OR s.original_title LIKE ? OR s.aliases LIKE ?)")
        like = f"%{q.strip()}%"
        params.extend([like, like, like])
    if root in {"library", "airing"}:
        where.append("s.root=?")
        params.append(root)
    if match:
        where.append("s.match_status=?")
        params.append(match)
    if airing == "1":
        where.append("s.is_airing=1")
    sql = (
        "SELECT s.*, "
        "(SELECT COUNT(*) FROM episodes e WHERE e.series_id=s.id) AS episode_count, "
        "(SELECT COUNT(*) FROM episodes e WHERE e.series_id=s.id AND e.status='present') AS present_count, "
        "(SELECT COUNT(*) FROM episodes e WHERE e.series_id=s.id AND e.status='missing') AS missing_count, "
        "(SELECT COUNT(*) FROM files f WHERE f.series_id=s.id AND f.kind='video' AND f.status='present') AS file_count "
        "FROM series s WHERE " + " AND ".join(where) + " ORDER BY s.title COLLATE NOCASE"
    )
    with session(cfg.db_path) as con:
        rows = [dict(row) for row in con.execute(sql, tuple(params)).fetchall()]
        unidentified = list_unidentified(con, cfg)
        folders = folder_choices(cfg)
    return render(
        request,
        "series_list.html",
        active="series",
        series=rows,
        unidentified=unidentified,
        folder_choices=folders,
        q=q,
        root=root,
        match=match,
        airing=airing,
    )


@app.get("/series/new", response_class=HTMLResponse)
def series_new(request: Request, carpeta: str = ""):
    with session(cfg.db_path) as con:
        unidentified = list_unidentified(con, cfg)
        selected = next(
            (item for item in unidentified if item["folder_name"] == carpeta),
            None,
        )
    info = parse_folder_info(carpeta) if carpeta else None
    suggested_title = (
        selected["series_title"]
        if selected
        else (info.title_guess if info else "")
    )
    suggested_year = (
        selected.get("series_year") if selected else (info.year if info else None)
    )
    return render(
        request,
        "series_new.html",
        active="series",
        carpeta=carpeta,
        unidentified=unidentified,
        folder_choices=folder_choices(cfg),
        suggested_title=suggested_title or "",
        suggested_year=suggested_year or "",
    )


@app.post("/series/new")
def series_new_save(
    folder_name: str = Form(...),
    title: str = Form(...),
    year: str = Form(""),
    tmdb_id: str = Form(""),
    tvmaze_id: str = Form(""),
    anilist_id: str = Form(""),
    series_key: str = Form(""),
    notes: str = Form(""),
):
    def number(value: str, label: str) -> int | None:
        value = value.strip()
        if not value:
            return None
        if not value.isdigit():
            raise ValueError(f"{label} debe ser numérico.")
        return int(value)

    try:
        year_value = int(year) if year.strip() else None
        tvmaze_value = number(tvmaze_id, "TVmaze ID")
        anilist_value = number(anilist_id, "AniList ID")
        result = identify_folder(
            cfg,
            folder_name=folder_name,
            title=title,
            year=year_value,
            tmdb_id=tmdb_id.strip() or None,
            tvmaze_id=tvmaze_value,
            anilist_id=anilist_value,
            series_key=series_key,
            notes=notes,
        )
    except Exception as exc:
        return redirect("/series/new", error=str(exc), carpeta=folder_name)
    return redirect(
        f"/series/{result['series_id']}",
        msg="Identificador guardado. La carpeta quedó vinculada a esta ficha.",
    )


@app.get("/series/{series_id}", response_class=HTMLResponse)
def series_detail(request: Request, series_id: int):
    with session(cfg.db_path) as con:
        series = fetch_series(con, series_id)
        seasons = [dict(row) for row in con.execute(
            "SELECT * FROM seasons WHERE series_id=? ORDER BY number", (series_id,)
        )]
        episodes = [dict(row) for row in con.execute(
            "SELECT * FROM episodes WHERE series_id=? ORDER BY season_number, episode_number",
            (series_id,),
        )]
        files = [dict(row) for row in con.execute(
            "SELECT * FROM files WHERE series_id=? AND status IN ('present','unparsed') "
            "ORDER BY CASE kind WHEN 'video' THEN 0 WHEN 'subtitle' THEN 1 ELSE 2 END, path LIMIT 500",
            (series_id,),
        )]
        candidates = [dict(row) for row in con.execute(
            "SELECT * FROM match_candidates WHERE series_id=? ORDER BY score DESC, provider LIMIT 30",
            (series_id,),
        )]
        issues = [dict(row) for row in con.execute(
            "SELECT * FROM issues WHERE series_id=? AND resolved=0 ORDER BY id DESC LIMIT 60",
            (series_id,),
        )]
        groups = [dict(row) for row in con.execute(
            "SELECT * FROM source_groups WHERE series_key=? ORDER BY position",
            (series["series_key"],),
        )]
        episode_files: dict[int, int] = {
            int(row["episode_id"]): int(row["c"])
            for row in con.execute(
                "SELECT episode_id, COUNT(*) c FROM files "
                "WHERE series_id=? AND kind='video' AND status='present' AND episode_id IS NOT NULL "
                "GROUP BY episode_id",
                (series_id,),
            )
        }
    by_season: dict[int, list[dict[str, Any]]] = {}
    for episode in episodes:
        item = dict(episode)
        item["file_count"] = episode_files.get(int(item["id"]), 0)
        by_season.setdefault(int(item["season_number"]), []).append(item)
    season_blocks = []
    for season in seasons:
        number = int(season["number"])
        items = by_season.get(number, [])
        season_blocks.append(
            {
                **season,
                "episodes": items,
                "present_count": sum(1 for e in items if e["status"] == "present"),
                "missing_count": sum(1 for e in items if e["status"] == "missing"),
            }
        )
    missing_labels = [
        f"S{int(e['season_number']):02d}E{float(e['episode_number']):g}"
        for e in episodes
        if e["status"] == "missing" and int(e["season_number"]) > 0
    ]
    return render(
        request,
        "series_detail.html",
        active="series",
        series=dict(series),
        seasons=season_blocks,
        files=files,
        candidates=candidates,
        issues=issues,
        groups=groups,
        missing_labels=missing_labels[:40],
        missing_count=len(missing_labels),
        episode_total=len(episodes),
        folder_choices=folder_choices(cfg),
    )


@app.post("/series/{series_id}/root")
def series_root(series_id: int, root: str = Form(...)):
    if root not in {"library", "airing"}:
        return redirect(f"/series/{series_id}", error="Destino inválido.")
    with session(cfg.db_path) as con:
        fetch_series(con, series_id)
        con.execute(
            "UPDATE series SET root=?, root_override=1, updated_at=? WHERE id=?",
            (root, now_iso(), series_id),
        )
    return redirect(
        f"/series/{series_id}",
        msg="Biblioteca de destino actualizada. Genera un plan nuevo para mover los archivos.",
    )


@app.post("/series/{series_id}/airing")
def series_airing(series_id: int, airing: str = Form("0")):
    with session(cfg.db_path) as con:
        fetch_series(con, series_id)
        con.execute(
            "UPDATE series SET is_airing=?, updated_at=? WHERE id=?",
            (1 if airing == "1" else 0, now_iso(), series_id),
        )
    return redirect(f"/series/{series_id}", msg="Estado de emisión actualizado.")


@app.post("/series/{series_id}/edit")
def series_edit(
    series_id: int,
    title: str = Form(...),
    year: str = Form(""),
    tmdb_id: str = Form(""),
    tvmaze_id: str = Form(""),
    anilist_id: str = Form(""),
    notes: str = Form(""),
):
    def numeric(value: str) -> str | None:
        value = value.strip()
        if not value:
            return None
        if not value.isdigit():
            raise ValueError(f"Identificador no numérico: {value}")
        return value

    try:
        year_value = int(year) if year.strip() else None
        tmdb_value = numeric(tmdb_id)
        tvmaze_value = numeric(tvmaze_id)
        anilist_value = numeric(anilist_id)
    except ValueError as exc:
        return redirect(f"/series/{series_id}", error=str(exc))
    with session(cfg.db_path) as con:
        fetch_series(con, series_id)
        con.execute(
            "UPDATE series SET title=?, year=?, tmdb_id=?, tvmaze_id=?, anilist_id=?, "
            "notes=?, match_status=CASE WHEN ? IS NOT NULL OR ? IS NOT NULL THEN 'manual' "
            "ELSE match_status END, match_confidence=1.0, last_synced=NULL, updated_at=? "
            "WHERE id=?",
            (
                title.strip(),
                year_value,
                tmdb_value,
                int(tvmaze_value) if tvmaze_value else None,
                int(anilist_value) if anilist_value else None,
                notes.strip() or None,
                tvmaze_value,
                tmdb_value,
                now_iso(),
                series_id,
            ),
        )
    return redirect(f"/series/{series_id}", msg="Ficha actualizada. Sincroniza para recargar episodios.")


@app.post("/series/{series_id}/folders")
def series_folders(series_id: int, folder_name: str = Form(...)):
    try:
        attach_folder_alias(cfg, series_id, folder_name.strip())
    except Exception as exc:
        return redirect(f"/series/{series_id}", error=str(exc))
    return redirect(
        f"/series/{series_id}",
        msg="Carpeta vinculada. El próximo escaneo usará esta ficha, sin crear duplicados.",
    )


@app.post("/series/{series_id}/search")
def series_search(series_id: int):
    with session(cfg.db_path) as con:
        fetch_series(con, series_id)
    return start_job_redirect("match_one", {"series_id": series_id})


@app.post("/series/{series_id}/select")
def series_select(series_id: int, candidate_id: int = Form(...)):
    from .matching import apply_candidate

    with session(cfg.db_path) as con:
        series = fetch_series(con, series_id)
        candidate = con.execute(
            "SELECT * FROM match_candidates WHERE id=? AND series_id=?",
            (candidate_id, series_id),
        ).fetchone()
        if not candidate:
            return redirect(f"/series/{series_id}", error="Candidato no encontrado.")
        apply_candidate(con, series, dict(candidate), status="manual")
    return redirect(
        f"/series/{series_id}",
        msg="Emparejamiento guardado. Sincroniza para recargar episodios.",
    )


# ---------------------------------------------------------------------------
# Planes
# ---------------------------------------------------------------------------
@app.get("/plans", response_class=HTMLResponse)
def plans_list(request: Request):
    with session(cfg.db_path) as con:
        rows = [dict(row) for row in con.execute(
            "SELECT * FROM plans ORDER BY id DESC LIMIT 200"
        )]
        for row in rows:
            row["summary_data"] = json_loads(row.get("summary"), {}) or {}
            row["action_count"] = con.execute(
                "SELECT COUNT(*) FROM plan_actions WHERE plan_id=?", (row["id"],)
            ).fetchone()[0]
    return render(request, "plans.html", active="plans", plans=rows)


@app.get("/plans/{plan_id}", response_class=HTMLResponse)
def plan_detail(
    request: Request,
    plan_id: int,
    action: str = "",
    q: str = "",
    page: int = 1,
):
    per_page = 250
    with session(cfg.db_path) as con:
        plan_row = con.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not plan_row:
            raise HTTPException(404, "Plan no encontrado")
        counts = {
            row["action"]: int(row["c"])
            for row in con.execute(
                "SELECT action, COUNT(*) c FROM plan_actions WHERE plan_id=? GROUP BY action",
                (plan_id,),
            )
        }
        where = ["a.plan_id=?"]
        params: list[Any] = [plan_id]
        if action in {"move", "rename", "quarantine", "conflict", "keep"}:
            where.append("a.action=?")
            params.append(action)
        if q.strip():
            where.append("(a.source LIKE ? OR a.target LIKE ? OR s.title LIKE ?)")
            like = f"%{q.strip()}%"
            params.extend([like, like, like])
        base = (
            "FROM plan_actions a LEFT JOIN series s ON s.id=a.series_id "
            "WHERE " + " AND ".join(where)
        )
        total = con.execute(f"SELECT COUNT(*) {base}", tuple(params)).fetchone()[0]
        pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(page, pages))
        actions = [dict(row) for row in con.execute(
            "SELECT a.*, s.title AS series_title, s.year AS series_year "
            f"{base} ORDER BY a.seq LIMIT ? OFFSET ?",
            (*params, per_page, (page - 1) * per_page),
        )]
        journal_count = con.execute(
            "SELECT COUNT(*) FROM journal WHERE plan_id=?", (plan_id,)
        ).fetchone()[0]
    plan = dict(plan_row)
    plan["summary_data"] = json_loads(plan.get("summary"), {}) or {}
    return render(
        request,
        "plan_detail.html",
        active="plans",
        plan=plan,
        counts=counts,
        actions=actions,
        total=total,
        page=page,
        pages=pages,
        action=action,
        q=q,
        journal_count=journal_count,
    )


@app.post("/plans/{plan_id}/apply")
def plan_apply(plan_id: int, confirm: str = Form("")):
    if confirm.strip().upper() != "APLICAR":
        return redirect(
            f"/plans/{plan_id}",
            error='Debes escribir exactamente "APLICAR" para confirmar.',
        )
    with session(cfg.db_path) as con:
        plan = con.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not plan:
            raise HTTPException(404, "Plan no encontrado")
        if plan["status"] not in {"ready"}:
            return redirect(
                f"/plans/{plan_id}",
                error=f"El plan está en estado {plan['status']} y no se puede aplicar.",
            )
    return start_job_redirect("apply", {"plan_id": plan_id})


@app.post("/plans/{plan_id}/undo")
def plan_undo(plan_id: int, confirm: str = Form("")):
    if confirm.strip().upper() != "DESHACER":
        return redirect(
            f"/plans/{plan_id}",
            error='Debes escribir exactamente "DESHACER" para confirmar.',
        )
    with session(cfg.db_path) as con:
        plan = con.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not plan:
            raise HTTPException(404, "Plan no encontrado")
        if plan["status"] not in {"applied", "partial", "applying"}:
            return redirect(
                f"/plans/{plan_id}",
                error="Este plan no tiene operaciones aplicadas que deshacer.",
            )
    return start_job_redirect("undo", {"plan_id": plan_id})


@app.get("/plans/{plan_id}/report")
def plan_report(plan_id: int):
    path = cfg.reports_root / f"plan_{plan_id}.html"
    if not path.exists():
        raise HTTPException(404, "Todavía no hay informe para este plan.")
    return FileResponse(path, media_type="text/html")


# ---------------------------------------------------------------------------
# Emisión
# ---------------------------------------------------------------------------
@app.get("/airing", response_class=HTMLResponse)
def airing_page(
    request: Request,
    temporada: str = "",
    ano: str = "",
    descubrir: str = "",
):
    info = season_info()
    season = (temporada or info["season"]).upper()
    if season not in SEASON_ES:
        season = info["season"]
    try:
        year = int(ano) if ano else info["year"]
    except ValueError:
        year = info["year"]
    discovered: list[dict[str, Any]] = []
    discovery_summary: dict[str, Any] = {}
    discover_error = None
    staging = staging_downloads(cfg)
    if descubrir:
        try:
            discovered = discover_season(cfg, season, year)
            discovery_summary = annotate_season_downloads(cfg, discovered, staging=staging)
        except Exception as exc:
            discover_error = str(exc)
    try:
        upcoming_items = upcoming(cfg, 30)
    except Exception:
        upcoming_items = []
    return render(
        request,
        "airing.html",
        active="airing",
        season=season,
        season_label=f"{SEASON_ES.get(season, season)} {year}",
        year=year,
        discovered=discovered,
        discovery_summary=discovery_summary,
        discover_error=discover_error,
        wants_discovery=bool(descubrir),
        staging_count=len(staging),
        staging_roots=[str(path) for path in cfg.staging_roots],
        library=library_airing(cfg),
        watchlist=list_watch(cfg),
        watch_statuses=WATCH_STATUSES,
        upcoming=upcoming_items,
    )


@app.post("/airing/watch/manual")
def airing_watch_manual(
    title: str = Form(...),
    total_episodes: str = Form(""),
    notes: str = Form(""),
    watch_status: str = Form("Viendo"),
):
    try:
        total = int(total_episodes) if total_episodes.strip() else None
        add_manual_watch(cfg, title, total_episodes=total, notes=notes, watch_status=watch_status)
    except Exception as exc:
        return redirect("/airing", error=f"No se pudo añadir: {exc}")
    return redirect("/airing", msg=f"Añadido a seguimiento manual: {title.strip()}")


@app.post("/airing/watch/bulk")
async def airing_watch_bulk(request: Request):
    form = await request.form()
    raw_ids = [str(value) for value in form.getlist("selected")]
    ids = [int(value) for value in raw_ids if value.isdigit()]
    season = str(form.get("temporada") or "").upper()
    year = str(form.get("ano") or "")
    if not ids:
        return redirect(
            "/airing", error="Selecciona al menos un anime.", descubrir="1",
            temporada=season, ano=year,
        )
    try:
        result = follow_anilist_ids(cfg, ids)
    except Exception as exc:
        return redirect(
            "/airing", error=f"No se pudo añadir la selección: {exc}", descubrir="1",
            temporada=season, ano=year,
        )
    msg = f"Añadidos {result['added']} títulos a seguimiento."
    if result["updated"]:
        msg += f" {result['updated']} ya estaban y se actualizaron."
    if result["missing"]:
        msg += f" {result['missing']} no se encontraron en AniList."
    return redirect(
        "/airing", msg=msg, descubrir="1", temporada=season, ano=year,
    )


@app.post("/airing/watch/{anilist_id}")
def airing_watch(anilist_id: int):
    try:
        media = AniListClient(cfg).media(anilist_id)
        add_watch(cfg, media)
    except Exception as exc:
        return redirect("/airing", error=f"No se pudo añadir: {exc}")
    return redirect("/airing", msg=f"Añadido a la lista de seguimiento: {media['title']}")


@app.get("/airing/watch/{watch_id}/edit", response_class=HTMLResponse)
def airing_watch_edit(request: Request, watch_id: int):
    with session(cfg.db_path) as con:
        row = con.execute("SELECT * FROM watchlist WHERE id=?", (watch_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Título de seguimiento no encontrado.")
    item = dict(row)
    item["genres_list"] = json_loads(item.get("genres"), []) or []
    return render(
        request,
        "watch_edit.html",
        active="airing",
        watch=item,
        watch_statuses=WATCH_STATUSES,
    )


@app.post("/airing/watch/{watch_id}/edit")
def airing_watch_save(
    watch_id: int,
    watch_status: str = Form("Viendo"),
    progress_episode: str = Form(""),
    rating: str = Form(""),
    notes: str = Form(""),
    title: str = Form(""),
    total_episodes: str = Form(""),
):
    try:
        with session(cfg.db_path) as con:
            row = con.execute("SELECT provider FROM watchlist WHERE id=?", (watch_id,)).fetchone()
        if not row:
            raise ValueError("Título de seguimiento no encontrado.")
        is_manual = row["provider"] == "manual"
        update_watch(
            cfg,
            watch_id,
            watch_status=watch_status,
            progress_episode=progress_episode,
            rating=rating,
            notes=notes,
            title=title if is_manual else None,
            total_episodes=total_episodes if is_manual else None,
        )
    except Exception as exc:
        return redirect(f"/airing/watch/{watch_id}/edit", error=str(exc))
    return redirect("/airing", msg="Seguimiento actualizado.")


@app.post("/airing/watch/{watch_id}/advance")
def airing_watch_advance(watch_id: int):
    try:
        item = advance_watch(cfg, watch_id)
    except Exception as exc:
        return redirect("/airing", error=f"No se pudo actualizar: {exc}")
    progress = item.get("progress_episode")
    return redirect("/airing", msg=f"Marcado el episodio {int(progress) if progress is not None else ''} como visto.")


@app.post("/airing/watch/{watch_id}/remove")
def airing_unwatch(watch_id: int):
    remove_watch(cfg, watch_id)
    return redirect("/airing", msg="Eliminado de la lista de seguimiento.")


@app.post("/airing/sync")
def airing_sync():
    return start_job_redirect("watch")


# ---------------------------------------------------------------------------
# Faltantes, duplicados, incidencias y cuarentena
# ---------------------------------------------------------------------------
@app.get("/missing", response_class=HTMLResponse)
def missing_page(
    request: Request,
    serie: int = 0,
    incluir_sin_emitir: str = "",
    todos: str = "",
):
    today = dt.date.today().isoformat()
    show_unaired = bool(incluir_sin_emitir)
    show_all = bool(todos)
    with session(cfg.db_path) as con:
        gaps = actionable_summary(con)
        if show_all:
            statuses = ("missing", "unaired") if show_unaired else ("missing",)
            placeholders = ",".join("?" for _ in statuses)
            rows = [dict(row) for row in con.execute(
                "SELECT s.id, s.title, s.year, s.root, s.cover_url, "
                "SUM(CASE WHEN e.status='missing' THEN 1 ELSE 0 END) AS missing_count, "
                "SUM(CASE WHEN e.status='unaired' THEN 1 ELSE 0 END) AS unaired_count, "
                "COUNT(e.id) AS total_count "
                "FROM episodes e JOIN series s ON s.id=e.series_id "
                f"WHERE e.status IN ({placeholders}) AND e.season_number>0 "
                "GROUP BY s.id HAVING COUNT(e.id)>0 "
                "ORDER BY missing_count DESC, s.title COLLATE NOCASE",
                statuses,
            )]
            total_missing = con.execute(
                "SELECT COUNT(*) FROM episodes WHERE status='missing' AND season_number>0 "
                "AND air_date IS NOT NULL AND air_date<=?",
                (today,),
            ).fetchone()[0]
        else:
            rows = gaps["rows"]
            for row in rows:
                row["unaired_count"] = 0
            total_missing = gaps["missing"]
        selected = None
        episodes = []
        if serie:
            selected = con.execute("SELECT * FROM series WHERE id=?", (serie,)).fetchone()
            if selected:
                if show_all:
                    statuses = ("missing", "unaired") if show_unaired else ("missing",)
                    placeholders = ",".join("?" for _ in statuses)
                    episodes = [dict(row) for row in con.execute(
                        f"SELECT * FROM episodes WHERE series_id=? AND status IN ({placeholders}) "
                        "AND season_number>0 ORDER BY season_number, episode_number",
                        (serie, *statuses),
                    )]
                else:
                    episodes = actionable_episodes(con, serie)
        summary = {
            "missing": total_missing,
            "all_missing": con.execute(
                "SELECT COUNT(*) FROM episodes WHERE status='missing' AND season_number>0 "
                "AND air_date IS NOT NULL AND air_date<=?",
                (today,),
            ).fetchone()[0],
            "unaired": con.execute(
                "SELECT COUNT(*) FROM episodes WHERE status='unaired' AND season_number>0"
            ).fetchone()[0],
            "gap_seasons": len([item for item in gaps["seasons"] if item["is_gap"]]),
        }
    return render(
        request,
        "missing.html",
        active="missing",
        rows=rows,
        selected=dict(selected) if selected else None,
        episodes=episodes,
        including_unaired=show_unaired,
        showing_all=show_all,
        summary=summary,
    )


@app.get("/duplicates", response_class=HTMLResponse)
def duplicates_page(request: Request):
    with session(cfg.db_path) as con:
        fingerprint_rows = con.execute(
            "SELECT fingerprint, COUNT(*) c, SUM(size) total FROM files "
            "WHERE kind='video' AND status IN ('present','unparsed') AND fingerprint IS NOT NULL "
            "AND fingerprint<>'' GROUP BY fingerprint HAVING c>1 "
            "ORDER BY total DESC"
        ).fetchall()
        exact_groups = []
        for group in fingerprint_rows:
            files = con.execute(
                "SELECT f.*, s.title AS series_title FROM files f "
                "LEFT JOIN series s ON s.id=f.series_id "
                "WHERE f.fingerprint=? AND f.kind='video' AND f.status IN ('present','unparsed') "
                "ORDER BY f.size DESC, f.path",
                (group["fingerprint"],),
            ).fetchall()
            exact_groups.append(
                {
                    "fingerprint": group["fingerprint"],
                    "count": group["c"],
                    "total": group["total"],
                    "files": [dict(row) for row in files],
                }
            )
        episode_groups = []
        for group in con.execute(
            "SELECT episode_id, COUNT(*) c FROM files "
            "WHERE kind='video' AND status='present' AND episode_id IS NOT NULL "
            "GROUP BY episode_id HAVING c>1 ORDER BY c DESC"
        ).fetchall():
            files = con.execute(
                "SELECT f.*, s.title AS series_title, e.season_number, e.episode_number, e.title AS episode_title "
                "FROM files f LEFT JOIN series s ON s.id=f.series_id "
                "LEFT JOIN episodes e ON e.id=f.episode_id "
                "WHERE f.episode_id=? AND f.kind='video' AND f.status='present' "
                "ORDER BY f.size DESC, f.path",
                (group["episode_id"],),
            ).fetchall()
            episode_groups.append(
                {
                    "episode_id": group["episode_id"],
                    "count": group["c"],
                    "files": [dict(row) for row in files],
                }
            )
    exact_keys = {
        file["path"]
        for group in exact_groups
        for file in group["files"]
    }
    return render(
        request,
        "duplicates.html",
        active="duplicates",
        exact_groups=exact_groups,
        episode_groups=episode_groups,
        exact_keys=exact_keys,
    )


@app.get("/issues", response_class=HTMLResponse)
def issues_page(request: Request, severity: str = "", code: str = "", resueltas: str = ""):
    where = ["1=1"]
    params: list[Any] = []
    if severity in {"critical", "error", "warning", "info"}:
        where.append("i.severity=?")
        params.append(severity)
    if code:
        where.append("i.code=?")
        params.append(code)
    if resueltas != "1":
        where.append("i.resolved=0")
    with session(cfg.db_path) as con:
        rows = [dict(row) for row in con.execute(
            "SELECT i.*, s.title AS series_title, f.path AS file_path "
            "FROM issues i LEFT JOIN series s ON s.id=i.series_id "
            "LEFT JOIN files f ON f.id=i.file_id "
            "WHERE " + " AND ".join(where) +
            " ORDER BY CASE i.severity WHEN 'critical' THEN 0 WHEN 'error' THEN 1 "
            "WHEN 'warning' THEN 2 ELSE 3 END, i.id DESC LIMIT 600",
            tuple(params),
        )]
        codes = [dict(row) for row in con.execute(
            "SELECT code, COUNT(*) c FROM issues WHERE resolved=0 GROUP BY code ORDER BY c DESC"
        )]
    return render(
        request,
        "issues.html",
        active="issues",
        issues=rows,
        codes=codes,
        severity=severity,
        code=code,
        resueltas=resueltas,
    )


@app.post("/issues/{issue_id}/resolve")
def issue_resolve(issue_id: int):
    with session(cfg.db_path) as con:
        con.execute("UPDATE issues SET resolved=1 WHERE id=?", (issue_id,))
    return redirect("/issues", msg="Incidencia marcada como resuelta.")


@app.post("/issues/resolve-all")
def issues_resolve_all():
    with session(cfg.db_path) as con:
        con.execute("UPDATE issues SET resolved=1 WHERE resolved=0")
    return redirect("/issues", msg="Todas las incidencias visibles se marcaron como resueltas.")


@app.get("/quarantine", response_class=HTMLResponse)
def quarantine_page(request: Request):
    entries: list[dict[str, Any]] = []
    total = 0
    if cfg.quarantine_root.exists():
        for path in sorted(cfg.quarantine_root.rglob("*")):
            if path.is_file():
                try:
                    stat = path.stat()
                except OSError:
                    continue
                total += stat.st_size
                entries.append(
                    {
                        "path": str(path),
                        "relative": str(path.relative_to(cfg.quarantine_root)),
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                    }
                )
    with session(cfg.db_path) as con:
        journal = [dict(row) for row in con.execute(
            "SELECT j.*, p.id AS plan_id FROM journal j LEFT JOIN plans p ON p.id=j.plan_id "
            "WHERE j.action='quarantine' ORDER BY j.id DESC LIMIT 100"
        )]
    return render(
        request,
        "quarantine.html",
        active="quarantine",
        entries=entries[:1200],
        total_entries=len(entries),
        total_size=total,
        journal=journal,
    )


# ---------------------------------------------------------------------------
# Trabajos
# ---------------------------------------------------------------------------
@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request):
    return render(
        request,
        "jobs.html",
        active="jobs",
        jobs_list=jobs.latest(cfg, 100),
    )


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: str):
    job = jobs.get(cfg, job_id)
    if not job:
        raise HTTPException(404, "Trabajo no encontrado")
    return render(request, "job_detail.html", active="jobs", job=job)


@app.post("/jobs/start/{kind}")
def job_start(
    kind: str,
    mode: str = Form("full"),
    fingerprint: str = Form("1"),
    plan_id: int = Form(0),
    series_id: int = Form(0),
):
    params: dict[str, Any] = {}
    if kind == "plan":
        params["mode"] = mode
    elif kind == "scan":
        params["fingerprint"] = fingerprint == "1"
    elif kind in {"apply", "undo"}:
        if not plan_id:
            return redirect("/plans", error="Falta el plan.")
        params["plan_id"] = int(plan_id)
    elif kind in {"match_one"}:
        if not series_id:
            return redirect("/series", error="Falta la serie.")
        params["series_id"] = int(series_id)
    elif kind not in {"scan", "sync", "match", "plan", "watch", "jellyfin_backup"}:
        return redirect("/", error="Tipo de trabajo no permitido.")
    return start_job_redirect(kind, params)


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    job = jobs.get(cfg, job_id)
    if not job:
        raise HTTPException(404, "Trabajo no encontrado")
    return JSONResponse(job)


@app.get("/api/jobs")
def api_jobs(active_only: int = 0, limit: int = 20):
    data = jobs.active(cfg) if active_only else jobs.latest(cfg, max(1, min(limit, 100)))
    return JSONResponse({"jobs": data})


# ---------------------------------------------------------------------------
# Ajustes
# ---------------------------------------------------------------------------
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    with session(cfg.db_path) as con:
        meta = {
            row["key"]: row["value"]
            for row in con.execute("SELECT key, value FROM meta")
        }
    return render(
        request,
        "settings.html",
        active="settings",
        public=cfg.as_public_dict(),
        staging_roots_text="\n".join(str(path) for path in cfg.staging_roots),
        meta=meta,
    )


@app.post("/settings")
def settings_save(
    library_root: str = Form(...),
    airing_root: str = Form(...),
    staging_roots: str = Form(""),
    include_tmdb_id: str = Form(""),
    include_episode_title: str = Form(""),
    include_quality: str = Form(""),
    keep_episode_thumbs: str = Form(""),
    auto_match_threshold: str = Form("0.80"),
    airing_horizon_days: str = Form("45"),
    jellyfin_url: str = Form(""),
    jellyfin_api_key: str = Form(""),
    jellyfin_db_path: str = Form(""),
    airing_library_name: str = Form("Anime en emisión"),
    airing_library_path: str = Form(""),
    tvmaze: str = Form(""),
    anilist: str = Form(""),
    web_host: str = Form("127.0.0.1"),
    web_port: str = Form("5055"),
):
    updates: dict[str, Any] = {
        "library_root": library_root.strip(),
        "airing_root": airing_root.strip(),
        "staging_roots": [
            line.strip()
            for line in staging_roots.replace(";", "\n").splitlines()
            if line.strip()
        ],
        "include_tmdb_id": bool(include_tmdb_id),
        "include_episode_title": bool(include_episode_title),
        "include_quality": bool(include_quality),
        "keep_episode_thumbs": bool(keep_episode_thumbs),
        "jellyfin": {
            "url": jellyfin_url.strip().rstrip("/"),
            "db_path": jellyfin_db_path.strip(),
            "airing_library_name": airing_library_name.strip() or "Anime en emisión",
            "airing_library_path": (airing_library_path.strip() or airing_root.strip()),
        },
        "providers": {"tvmaze": bool(tvmaze), "anilist": bool(anilist)},
        "web": {"host": web_host.strip() or "127.0.0.1", "port": int(web_port or 5055)},
    }
    if jellyfin_api_key.strip() and jellyfin_api_key.strip() != "********":
        updates["jellyfin"]["api_key"] = jellyfin_api_key.strip()
    try:
        updates["auto_match_threshold"] = max(0.1, min(1.0, float(auto_match_threshold)))
        updates["airing_horizon_days"] = max(7, int(airing_horizon_days))
        updates["web"]["port"] = max(1024, min(65535, int(web_port)))
    except ValueError:
        return redirect("/settings", error="Revisa los valores numéricos.")
    cfg.update(updates)
    cfg.save()
    cfg.ensure_dirs()
    return redirect("/settings", msg="Ajustes guardados. Reinicia el panel si cambiaste el puerto.")


@app.post("/settings/test/jellyfin")
def settings_test_jellyfin():
    try:
        result = JellyfinClient(cfg).test()
        msg = (
            f"Jellyfin responde: {result['server']} {result['version']} · "
            f"usuarios: {', '.join(result['users']) or '—'} · "
            f"bibliotecas: {', '.join(lib['name'] for lib in result['libraries']) or '—'}"
        )
    except Exception as exc:
        return redirect("/settings", error=f"Jellyfin: {exc}")
    return redirect("/settings", msg=msg)


@app.post("/settings/test/providers")
def settings_test_providers():
    messages = []
    errors = []
    try:
        shows = TVmazeClient(cfg).search("Cowboy Bebop", limit=1)
        messages.append(f"TVmaze OK ({shows[0]['title'] if shows else 'sin resultados'})")
    except Exception as exc:
        errors.append(f"TVmaze: {exc}")
    try:
        items = AniListClient(cfg).search("Cowboy Bebop", limit=1)
        messages.append(f"AniList OK ({items[0]['title'] if items else 'sin resultados'})")
    except Exception as exc:
        errors.append(f"AniList: {exc}")
    if errors:
        return redirect("/settings", msg="; ".join(messages), error="; ".join(errors))
    return redirect("/settings", msg="; ".join(messages))


@app.post("/settings/jellyfin/create-library")
def settings_create_library():
    try:
        result = JellyfinClient(cfg).ensure_airing_library()
    except Exception as exc:
        return redirect("/settings", error=f"No se pudo crear la biblioteca: {exc}")
    if result.get("created"):
        return redirect("/settings", msg="Biblioteca «Anime en emisión» creada en Jellyfin.")
    return redirect("/settings", msg="La biblioteca de emisión ya existe en Jellyfin.")


@app.post("/settings/jellyfin/refresh")
def settings_refresh_library():
    try:
        JellyfinClient(cfg).refresh_library()
    except Exception as exc:
        return redirect("/settings", error=f"No se pudo refrescar Jellyfin: {exc}")
    return redirect("/settings", msg="Jellyfin inició el refresco de la biblioteca.")


@app.post("/settings/jellyfin/backup")
def settings_backup_watch():
    return start_job_redirect("jellyfin_backup")


@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request):
    files = []
    for path in sorted(cfg.reports_root.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True):
        files.append(
            {
                "name": path.name,
                "size": path.stat().st_size,
                "mtime": path.stat().st_mtime,
                "url": f"/reports/{path.name}",
            }
        )
    return render(request, "reports.html", active="reports", files=files)


@app.get("/health")
def health():
    return {"ok": True, "app": "SeriesAdmin", "db": str(cfg.db_path), "library": str(cfg.library_root)}
