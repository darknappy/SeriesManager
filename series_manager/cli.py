"""Interfaz de línea de comandos en español."""
from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import webbrowser
from pathlib import Path
from typing import Any

import click
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import __version__, jobs
from .airing import (
    WATCH_STATUSES,
    add_manual_watch,
    advance_watch,
    annotate_season_downloads,
    discover_season,
    follow_anilist_ids,
    library_airing,
    list_watch,
    remove_watch,
    season_info,
    staging_downloads,
    sync_watchlist,
    update_watch,
    upcoming,
)
from .apply import PlanError, apply_plan, undo_plan
from .catalog import attach_folder_alias, identify_folder, list_unidentified
from .config import Config
from .db import init_db, session
from .jellyfin import JellyfinClient, backup_watch_state
from .matching import refresh_all
from .missing import actionable_episodes, actionable_summary
from .naming import folder_name_for_series
from .plan import build_plan
from .providers.anilist import AniListClient, SEASON_ES
from .providers.tvmaze import TVmazeClient
from .reports import write_plan_report
from .scan import scan_library
from .seeds import apply_all
from .sync import sync_metadata
from .utils import format_date_es, format_datetime_es, human_size, json_loads

if sys.platform == "win32":
    # La consola de Windows no siempre usa UTF-8; evitamos errores con tildes y flechas.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

console = Console()


def _json(value: Any) -> None:
    console.print_json(json.dumps(value, ensure_ascii=False, default=str))


def _run_with_progress(cfg: Config, kind: str, params: dict[str, Any], title: str) -> Any:
    with Progress(
        SpinnerColumn(style="cyan"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=38),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task(title, total=100)

        def callback(pct: int, message: str) -> None:
            progress.update(task, completed=pct, description=message[:90])

        result = jobs.run(cfg, kind, params, callback)
    if not result.get("ok"):
        console.print(Panel(str(result.get("error")), title="Error", border_style="red"))
        raise SystemExit(1)
    return result["result"]


def _open_path(path: Path) -> None:
    try:
        webbrowser.open(path.resolve().as_uri())
    except Exception as exc:
        console.print(f"[yellow]No se pudo abrir el navegador: {exc}[/yellow]")


def _table(title: str, columns: list[tuple[str, dict[str, Any]]]) -> Table:
    table = Table(title=title, box=box.SIMPLE_HEAVY, header_style="bold cyan", expand=False)
    for name, options in columns:
        table.add_column(name, **options)
    return table


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Ruta alternativa a config.json.",
)
@click.version_option(__version__, prog_name="SeriesAdmin")
@click.pass_context
def main(ctx: click.Context, config_path: Path | None) -> None:
    """SeriesAdmin: ordena tu biblioteca de series para Jellyfin sin perder archivos.

    Flujo recomendado: init → scan → sync → plan → revisar el informe → apply.
    """
    cfg = Config(config_path)
    ctx.obj = cfg


# ---------------------------------------------------------------------------
# Preparación y análisis
# ---------------------------------------------------------------------------
@main.command("init")
@click.pass_obj
def cmd_init(cfg: Config) -> None:
    """Crea carpetas, base de datos y semillas."""
    cfg.ensure_dirs()
    init_db(cfg.db_path)
    stats = apply_all(cfg)
    if not cfg.path.exists():
        cfg.save()
    console.print(Panel.fit(
        f"[bold green]SeriesAdmin listo[/bold green]\n"
        f"Programa: [cyan]{cfg.app_root}[/cyan]\n"
        f"Biblioteca: [cyan]{cfg.library_root}[/cyan]\n"
        f"En emisión: [cyan]{cfg.airing_root}[/cyan]\n"
        f"Base de datos: [cyan]{cfg.db_path}[/cyan]\n"
        f"Semillas: {stats['series_created']} series creadas, {stats['series_updated']} actualizadas, "
        f"{stats['groups']} carpetas.",
        title="Inicialización",
        border_style="green",
    ))


@main.command("scan")
@click.option("--sin-huellas", is_flag=True, help="No calcular huellas de archivos (más rápido).")
@click.option("--json", "as_json", is_flag=True, help="Mostrar el resultado en JSON.")
@click.pass_obj
def cmd_scan(cfg: Config, sin_huellas: bool, as_json: bool) -> None:
    """Escanea D:\\Series y clasifica los archivos."""
    result = _run_with_progress(cfg, "scan", {"fingerprint": not sin_huellas}, "Escaneando la biblioteca")
    if as_json:
        _json(result)
        return
    table = _table("Resultado del escaneo", [("Métrica", {"style": "cyan"}), ("Valor", {"justify": "right"})])
    labels = {
        "folders": "Carpetas",
        "videos": "Vídeos",
        "parsed": "Vídeos interpretados",
        "unparsed": "Vídeos sin interpretar",
        "specials": "Especiales",
        "sidecars": "Subtítulos/NFO",
        "images": "Imágenes",
        "missing": "Archivos desaparecidos",
        "duplicates": "Grupos duplicados",
        "merged_series": "Fichas duplicadas fusionadas",
        "staging_cleaned": "Fichas de descargas retiradas",
    }
    for key, label in labels.items():
        table.add_row(label, str(result.get(key, 0)))
    console.print(table)
    console.print("[dim]Los archivos no se han modificado.[/dim]")


@main.command("sync")
@click.option("--forzar", is_flag=True, help="Ignorar la caché HTTP y consultar TVmaze de nuevo.")
@click.option("--json", "as_json", is_flag=True, help="Mostrar el resultado en JSON.")
@click.pass_obj
def cmd_sync(cfg: Config, forzar: bool, as_json: bool) -> None:
    """Sincroniza temporadas, episodios y estado de emisión con TVmaze."""
    result = _run_with_progress(cfg, "sync", {"force": forzar}, "Sincronizando metadatos")
    if as_json:
        _json(result)
        return
    table = _table("Sincronización", [("Métrica", {"style": "cyan"}), ("Valor", {"justify": "right"})])
    table.add_row("Series sincronizadas", str(result.get("synced", 0)))
    table.add_row("Sin proveedor", str(result.get("without_provider", 0)))
    table.add_row("Episodios", str(result.get("episodes", 0)))
    table.add_row("Archivos asignados", str(result.get("assigned", 0)))
    table.add_row("Sin asignar", str(result.get("unassigned", 0)))
    table.add_row("Episodios faltantes", str(result.get("missing", 0)))
    console.print(table)
    errors = result.get("errors") or []
    if errors:
        console.print(Panel("\n".join(errors[:12]), title="Errores de proveedor", border_style="yellow"))


@main.command("match")
@click.option("--json", "as_json", is_flag=True, help="Mostrar el resultado en JSON.")
@click.pass_obj
def cmd_match(cfg: Config, as_json: bool) -> None:
    """Busca emparejamientos en TVmaze y AniList para las series sin proveedor."""
    result = _run_with_progress(cfg, "match", {}, "Buscando emparejamientos")
    if as_json:
        _json(result)
        return
    console.print(
        f"Series revisadas: [bold]{result.get('series', 0)}[/bold] · "
        f"emparejadas: [green]{result.get('matched', 0)}[/green] · "
        f"para revisar: [yellow]{result.get('review', 0)}[/yellow] · "
        f"candidatos: {result.get('candidates', 0)}"
    )


@main.command("rebuild")
@click.option("--sin-escaneo", is_flag=True, help="No volver a escanear.")
@click.option("--sin-sync", is_flag=True, help="No volver a sincronizar.")
@click.option("--sin-plan", is_flag=True, help="No generar el plan al final.")
@click.option("--json", "as_json", is_flag=True, help="Mostrar el resultado en JSON.")
@click.pass_obj
def cmd_rebuild(cfg: Config, sin_escaneo: bool, sin_sync: bool, sin_plan: bool, as_json: bool) -> None:
    """Reconstruye el catálogo: escaneo + sincronización + plan de previsualización."""
    result: dict[str, Any] = {}
    if not sin_escaneo:
        result["scan"] = _run_with_progress(cfg, "scan", {"fingerprint": True}, "Escaneando")
    if not sin_sync:
        result["sync"] = _run_with_progress(cfg, "sync", {"force": False}, "Sincronizando")
    if not sin_plan:
        result["plan"] = _run_with_progress(cfg, "plan", {"mode": "full"}, "Generando plan")
    if as_json:
        _json(result)
        return
    console.print("[bold green]Reconstrucción terminada.[/bold green]")
    plan = result.get("plan") or {}
    if plan:
        console.print(
            f"Plan [bold]#{plan['plan_id']}[/bold]: "
            f"{plan['summary'].get('move', 0)} movimientos, "
            f"{plan['summary'].get('quarantine', 0)} a cuarentena, "
            f"{plan['summary'].get('conflict', 0)} conflictos.\n"
            f"Informe: [cyan]{plan.get('report') or 'no generado'}[/cyan]"
        )
        console.print("[yellow]Nada se ha modificado. Revisa el informe y aplica con 'apply <id>'.[/yellow]")


@main.command("plan")
@click.option("--modo", type=click.Choice(["full", "quick"]), default="full", show_default=True)
@click.option("--abrir", is_flag=True, help="Abrir el informe HTML al terminar.")
@click.option("--json", "as_json", is_flag=True, help="Mostrar el resultado en JSON.")
@click.pass_obj
def cmd_plan(cfg: Config, modo: str, abrir: bool, as_json: bool) -> None:
    """Genera un plan de renombrado/movimiento en seco y su informe HTML."""
    result = _run_with_progress(cfg, "plan", {"mode": modo}, "Generando plan")
    if as_json:
        _json(result)
    else:
        summary = result["summary"]
        table = _table(
            f"Plan #{result['plan_id']}",
            [("Acción", {"style": "cyan"}), ("Cantidad", {"justify": "right"})],
        )
        table.add_row("Movimientos/renombrados", str(summary.get("move", 0)))
        table.add_row("A cuarentena", str(summary.get("quarantine", 0)))
        table.add_row("Conflictos", str(summary.get("conflict", 0)))
        table.add_row("Conservados", str(summary.get("keep", 0)))
        table.add_row("Series", str(summary.get("series", 0)))
        table.add_row("Volumen", human_size(summary.get("bytes", 0)))
        console.print(table)
        console.print(f"Informe: [cyan]{result.get('report') or 'no generado'}[/cyan]")
        console.print("[yellow]Previsualización solamente. No se ha movido ningún archivo.[/yellow]")
    if abrir and result.get("report"):
        _open_path(Path(result["report"]))


@main.command("apply")
@click.argument("plan_id", type=int)
@click.option("--si", is_flag=True, help="Confirmar sin pregunta interactiva.")
@click.pass_obj
def cmd_apply(cfg: Config, plan_id: int, si: bool) -> None:
    """Aplica un plan previamente revisado (requiere confirmación explícita)."""
    with session(cfg.db_path) as con:
        plan = con.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not plan:
            raise click.ClickException(f"No existe el plan {plan_id}.")
        if plan["status"] != "ready":
            raise click.ClickException(f"El plan está en estado {plan['status']} y no se puede aplicar.")
        summary = json_loads(plan["summary"], {}) or {}
    console.print(
        Panel(
            f"Plan #{plan_id}: {summary.get('move', 0)} movimientos, "
            f"{summary.get('quarantine', 0)} a cuarentena, "
            f"{summary.get('conflict', 0)} conflictos.\n"
            "Los archivos se moverán de verdad. Cada operación queda en el diario.",
            title="Confirmación",
            border_style="yellow",
        )
    )
    if not si:
        click.confirm("¿Aplicar el plan ahora?", abort=True)
    result = _run_with_progress(cfg, "apply", {"plan_id": plan_id}, "Aplicando plan")
    table = _table("Aplicación", [("Resultado", {"style": "cyan"}), ("Cantidad", {"justify": "right"})])
    table.add_row("Operaciones hechas", str(result.get("done", 0)))
    table.add_row("Omitidas", str(result.get("skipped", 0)))
    table.add_row("Errores", str(result.get("errors", 0)))
    table.add_row("Volumen", human_size(result.get("bytes", 0)))
    console.print(table)
    for message in (result.get("messages") or [])[:10]:
        console.print(f"[red]· {message}[/red]")
    console.print(f"Puedes revertir con: [cyan]python -m series_manager undo {plan_id}[/cyan]")


@main.command("undo")
@click.argument("plan_id", type=int)
@click.option("--si", is_flag=True, help="Confirmar sin pregunta interactiva.")
@click.pass_obj
def cmd_undo(cfg: Config, plan_id: int, si: bool) -> None:
    """Deshace un plan aplicado usando el diario de operaciones."""
    with session(cfg.db_path) as con:
        plan = con.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not plan:
            raise click.ClickException(f"No existe el plan {plan_id}.")
        if plan["status"] not in {"applied", "partial", "applying"}:
            raise click.ClickException("El plan no tiene operaciones aplicadas.")
    if not si:
        click.confirm(f"¿Deshacer el plan {plan_id} y devolver los archivos a su sitio?", abort=True)
    result = _run_with_progress(cfg, "undo", {"plan_id": plan_id}, "Deshaciendo plan")
    console.print(
        f"Operaciones revertidas: [green]{result.get('done', 0)}[/green] · "
        f"errores: [red]{result.get('errors', 0)}[/red] · "
        f"omitidas: {result.get('skipped', 0)}"
    )
    for message in (result.get("messages") or [])[:10]:
        console.print(f"[red]· {message}[/red]")


@main.command("report")
@click.argument("plan_id", type=int)
@click.option("--abrir", is_flag=True, help="Abrir el informe en el navegador.")
@click.pass_obj
def cmd_report(cfg: Config, plan_id: int, abrir: bool) -> None:
    """Genera (o regenera) el informe HTML de un plan."""
    path = write_plan_report(cfg, plan_id)
    console.print(f"Informe: [cyan]{path}[/cyan]")
    if abrir:
        _open_path(path)


# ---------------------------------------------------------------------------
# Consultas
# ---------------------------------------------------------------------------
@main.command("status")
@click.option("--json", "as_json", is_flag=True, help="Mostrar el resultado en JSON.")
@click.pass_obj
def cmd_status(cfg: Config, as_json: bool) -> None:
    """Resumen del catálogo, incidencias y último plan."""
    with session(cfg.db_path) as con:
        def scalar(sql: str, params: tuple = ()) -> int:
            return int(con.execute(sql, params).fetchone()[0] or 0)

        data: dict[str, Any] = {
            "series": scalar("SELECT COUNT(*) FROM series"),
            "episodes": scalar("SELECT COUNT(*) FROM episodes"),
            "present": scalar("SELECT COUNT(*) FROM episodes WHERE status='present'"),
            "missing": scalar("SELECT COUNT(*) FROM episodes WHERE status='missing'"),
            "unaired": scalar("SELECT COUNT(*) FROM episodes WHERE status='unaired'"),
            "videos": scalar("SELECT COUNT(*) FROM files WHERE kind='video' AND status='present'"),
            "bytes": scalar("SELECT COALESCE(SUM(size),0) FROM files WHERE kind='video' AND status='present'"),
            "issues": scalar("SELECT COUNT(*) FROM issues WHERE resolved=0"),
            "unmatched": scalar("SELECT COUNT(*) FROM series WHERE match_status IN ('unmatched','seeded')"),
            "airing": scalar("SELECT COUNT(*) FROM series WHERE is_airing=1"),
            "watch": scalar("SELECT COUNT(*) FROM watchlist"),
            "plans": scalar("SELECT COUNT(*) FROM plans"),
        }
        meta = {row["key"]: row["value"] for row in con.execute("SELECT key, value FROM meta")}
        latest = con.execute("SELECT * FROM plans ORDER BY id DESC LIMIT 1").fetchone()
        by_code = [dict(row) for row in con.execute(
            "SELECT code, COUNT(*) c FROM issues WHERE resolved=0 GROUP BY code ORDER BY c DESC LIMIT 8"
        )]
    if as_json:
        data["last_scan"] = meta.get("last_scan")
        data["last_sync"] = meta.get("last_sync")
        data["latest_plan"] = dict(latest) if latest else None
        data["issue_codes"] = by_code
        _json(data)
        return
    console.print(Panel.fit(
        f"[bold]SeriesAdmin {__version__}[/bold]\n"
        f"Biblioteca: [cyan]{cfg.library_root}[/cyan]\n"
        f"En emisión: [cyan]{cfg.airing_root}[/cyan]",
        border_style="cyan",
    ))
    table = _table("Catálogo", [("Métrica", {"style": "cyan"}), ("Valor", {"justify": "right"})])
    rows = [
        ("Series", data["series"]),
        ("Episodios", data["episodes"]),
        ("Presentes", data["present"]),
        ("Faltantes", data["missing"]),
        ("Sin emitir", data["unaired"]),
        ("Archivos de vídeo", data["videos"]),
        ("Tamaño", human_size(data["bytes"])),
        ("En emisión", data["airing"]),
        ("Lista de seguimiento", data["watch"]),
        ("Incidencias abiertas", data["issues"]),
        ("Series por revisar", data["unmatched"]),
        ("Planes", data["plans"]),
    ]
    for label, value in rows:
        table.add_row(label, str(value))
    console.print(table)
    console.print(f"Último escaneo: {format_datetime_es(meta.get('last_scan'))}")
    console.print(f"Última sincronización: {format_datetime_es(meta.get('last_sync'))}")
    if latest:
        summary = json_loads(latest["summary"], {}) or {}
        console.print(
            f"Último plan: [bold]#{latest['id']}[/bold] ({latest['status']}) · "
            f"{summary.get('move', 0)} movimientos · {summary.get('quarantine', 0)} cuarentena"
        )
    if by_code:
        console.print("[yellow]Incidencias por código:[/yellow] " + ", ".join(
            f"{row['code']} ({row['c']})" for row in by_code
        ))


@main.command("list")
@click.option("--q", "query", default="", help="Filtrar por título.")
@click.option("--limite", type=int, default=200, show_default=True)
@click.pass_obj
def cmd_list(cfg: Config, query: str, limite: int) -> None:
    """Lista las series del catálogo."""
    sql = (
        "SELECT s.*, "
        "(SELECT COUNT(*) FROM episodes e WHERE e.series_id=s.id) eps, "
        "(SELECT COUNT(*) FROM episodes e WHERE e.series_id=s.id AND e.status='present') present, "
        "(SELECT COUNT(*) FROM files f WHERE f.series_id=s.id AND f.kind='video' AND f.status='present') files "
        "FROM series s "
    )
    params: list[Any] = []
    if query:
        sql += "WHERE s.title LIKE ? OR s.original_title LIKE ? OR s.aliases LIKE ? "
        like = f"%{query}%"
        params = [like, like, like]
    sql += "ORDER BY s.title COLLATE NOCASE LIMIT ?"
    params.append(max(1, limite))
    with session(cfg.db_path) as con:
        rows = con.execute(sql, tuple(params)).fetchall()
    table = _table("Series", [
        ("Serie", {"style": "bold"}),
        ("Año", {"justify": "right"}),
        ("Biblioteca", {}),
        ("Episodios", {"justify": "right"}),
        ("Archivos", {"justify": "right"}),
        ("Emparejamiento", {}),
    ])
    for row in rows:
        table.add_row(
            row["title"],
            str(row["year"] or "—"),
            row["root"],
            f"{row['present']}/{row['eps']}",
            str(row["files"]),
            row["match_status"],
        )
    console.print(table)
    console.print(f"[dim]{len(rows)} series mostradas.[/dim]")


def _series_by_reference(cfg: Config, reference: str):
    with session(cfg.db_path) as con:
        if reference.isdigit():
            return con.execute("SELECT * FROM series WHERE id=?", (int(reference),)).fetchone()
        row = con.execute(
            "SELECT * FROM series WHERE series_key=?", (reference,)
        ).fetchone()
        if row:
            return row
        return con.execute(
            "SELECT * FROM series WHERE title LIKE ? OR original_title LIKE ? "
            "ORDER BY title LIMIT 1",
            (f"%{reference}%", f"%{reference}%"),
        ).fetchone()


@main.command("unidentified")
@click.pass_obj
def cmd_unidentified(cfg: Config) -> None:
    """Lista carpetas que aún no tienen una ficha reconocible."""
    with session(cfg.db_path) as con:
        rows = list_unidentified(con, cfg)
    table = _table("Carpetas sin identificar", [
        ("Carpeta", {"style": "bold"}), ("Ficha provisional", {}),
        ("Vídeos", {"justify": "right"}), ("Serie", {"justify": "right"}),
    ])
    for row in rows:
        table.add_row(
            row["folder_name"],
            row.get("series_title") or "—",
            str(row.get("video_count") or 0),
            str(row.get("series_id") or "—"),
        )
    console.print(table)
    if rows:
        console.print(
            "[dim]Identifica una carpeta con: python -m series_manager identify "
            "\"NOMBRE DE CARPETA\" --titulo \"Título\" --tmdb 123[/dim]"
        )


@main.command("identify")
@click.argument("folder")
@click.option("--titulo", "title", required=True, help="Título real de la serie.")
@click.option("--ano", "year", type=int, default=None, help="Año de estreno.")
@click.option("--tmdb", "tmdb_id", default="", help="TMDB ID.")
@click.option("--tvmaze", "tvmaze_id", type=int, default=None, help="TVmaze ID.")
@click.option("--anilist", "anilist_id", type=int, default=None, help="AniList ID.")
@click.option("--clave", "series_key", default="", help="Identificador local opcional.")
@click.option("--notas", "notes", default="", help="Notas para la ficha.")
@click.pass_obj
def cmd_identify(
    cfg: Config,
    folder: str,
    title: str,
    year: int | None,
    tmdb_id: str,
    tvmaze_id: int | None,
    anilist_id: int | None,
    series_key: str,
    notes: str,
) -> None:
    """Crea un identificador manual para una carpeta con nombre modificado."""
    try:
        result = identify_folder(
            cfg,
            folder_name=folder,
            title=title,
            year=year,
            tmdb_id=tmdb_id or None,
            tvmaze_id=tvmaze_id,
            anilist_id=anilist_id,
            series_key=series_key,
            notes=notes,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    action = "creada" if result["created"] else "actualizada"
    console.print(
        f"[green]Ficha {action}:[/green] {title} (id {result['series_id']}, "
        f"clave {result['series_key']}). Carpeta: {folder}"
    )
    if result.get("merged"):
        console.print("[dim]La ficha provisional anterior se fusionó con la canónica.[/dim]")


@main.command("link-folder")
@click.argument("serie")
@click.argument("folder")
@click.pass_obj
def cmd_link_folder(cfg: Config, serie: str, folder: str) -> None:
    """Vincula una carpeta renombrada a una ficha existente (id, clave o título)."""
    row = _series_by_reference(cfg, serie)
    if not row:
        raise click.ClickException(f"No se encontró la serie: {serie}")
    try:
        result = attach_folder_alias(cfg, int(row["id"]), folder)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    console.print(
        f"[green]Carpeta vinculada:[/green] {folder} → {row['title']} "
        f"(id {result['series_id']})"
    )


@main.command("downloads")
@click.pass_obj
def cmd_downloads(cfg: Config) -> None:
    """Lista los vídeos detectados en las carpetas de descargas en curso."""
    entries = staging_downloads(cfg)
    table = _table("Descargas en curso", [
        ("Episodio", {}), ("Archivo", {"style": "dim"}), ("Título detectado", {}),
    ])
    for entry in entries:
        table.add_row(
            entry["episode_label"],
            entry["relative"],
            " / ".join(entry["titles"][:2]) or "—",
        )
    console.print(table)
    console.print(f"[dim]{len(entries)} vídeos en {len(cfg.staging_roots)} carpeta(s).[/dim]")


@main.command("missing")
@click.option("--serie", "serie", default="", help="Ver el detalle de una serie (id o texto).")
@click.option("--todos", is_flag=True, help="Incluir temporadas ausentes y episodios iniciales ya vistos.")
@click.option("--limite", type=int, default=80, show_default=True)
@click.pass_obj
def cmd_missing(cfg: Config, serie: str, todos: bool, limite: int) -> None:
    """Lista huecos aislados; con --todos, todos los episodios faltantes."""
    with session(cfg.db_path) as con:
        gaps = actionable_summary(con)
        if todos:
            rows = con.execute(
                "SELECT s.id, s.title, s.year, "
                "SUM(CASE WHEN e.status='missing' THEN 1 ELSE 0 END) missing, "
                "COUNT(e.id) total "
                "FROM episodes e JOIN series s ON s.id=e.series_id "
                "WHERE e.status='missing' AND e.season_number>0 AND e.air_date IS NOT NULL "
                "AND e.air_date<=date('now') GROUP BY s.id "
                "ORDER BY missing DESC, s.title LIMIT ?",
                (limite,),
            ).fetchall()
            total_missing = sum(int(row["missing"]) for row in rows)
        else:
            rows = gaps["rows"][:limite]
            total_missing = gaps["missing"]
        selected = None
        episodes = []
        if serie:
            if serie.isdigit():
                selected = con.execute("SELECT * FROM series WHERE id=?", (int(serie),)).fetchone()
            if not selected:
                selected = con.execute(
                    "SELECT * FROM series WHERE title LIKE ? ORDER BY title LIMIT 1",
                    (f"%{serie}%",),
                ).fetchone()
            if selected:
                if todos:
                    episodes = con.execute(
                        "SELECT * FROM episodes WHERE series_id=? AND status='missing' "
                        "AND season_number>0 ORDER BY season_number, episode_number",
                        (selected["id"],),
                    ).fetchall()
                else:
                    episodes = actionable_episodes(con, int(selected["id"]))
    title = "Todos los episodios faltantes" if todos else "Huecos aislados en temporadas ya empezadas"
    if not todos:
        console.print(
            "[dim]Las temporadas ausentes y los episodios iniciales ya vistos se ignoran. "
            "Usa --todos para verlos.[/dim]"
        )
    table = _table(title, [
        ("ID", {"justify": "right"}), ("Serie", {"style": "bold"}),
        ("Año", {"justify": "right"}), ("Faltan", {"justify": "right"}),
    ])
    for row in rows:
        table.add_row(str(row["id"]), row["title"], str(row["year"] or "—"), str(row["missing"] if todos else row["missing_count"]))
    console.print(table)
    console.print(f"[dim]{total_missing} episodios en total.[/dim]")
    if selected:
        detail = _table(f"Faltantes de {selected['title']}", [
            ("Episodio", {}), ("Título", {}), ("Emisión", {}),
        ])
        for episode in episodes:
            detail.add_row(
                f"S{int(episode['season_number']):02d}E{float(episode['episode_number']):g}",
                episode["title"] or "—",
                format_date_es(episode["air_date"]),
            )
        console.print(detail)


@main.command("duplicates")
@click.option("--limite", type=int, default=25, show_default=True)
@click.pass_obj
def cmd_duplicates(cfg: Config, limite: int) -> None:
    """Muestra archivos idénticos y episodios con varios archivos."""
    with session(cfg.db_path) as con:
        groups = con.execute(
            "SELECT fingerprint, COUNT(*) c, SUM(size) total FROM files "
            "WHERE kind='video' AND status='present' AND fingerprint IS NOT NULL "
            "AND fingerprint<>'' GROUP BY fingerprint HAVING c>1 ORDER BY total DESC LIMIT ?",
            (limite,),
        ).fetchall()
        episode_groups = con.execute(
            "SELECT episode_id, COUNT(*) c FROM files WHERE kind='video' AND status='present' "
            "AND episode_id IS NOT NULL GROUP BY episode_id HAVING c>1 ORDER BY c DESC"
        ).fetchall()
    if not groups:
        console.print("[green]No se han detectado archivos idénticos.[/green]")
    for group in groups:
        with session(cfg.db_path) as con:
            files = con.execute(
                "SELECT f.path, f.size, s.title FROM files f LEFT JOIN series s ON s.id=f.series_id "
                "WHERE f.fingerprint=? AND f.kind='video' AND f.status='present'",
                (group["fingerprint"],),
            ).fetchall()
        table = _table(
            f"Duplicado exacto · {group['c']} copias · {human_size(group['total'])}",
            [("Serie", {}), ("Ruta", {"style": "dim"}), ("Tamaño", {"justify": "right"})],
        )
        for file in files:
            table.add_row(file["title"] or "—", file["path"], human_size(file["size"]))
        console.print(table)
    console.print(f"[dim]{len(episode_groups)} episodios tienen más de un archivo.[/dim]")


@main.command("airing")
@click.option("--temporada", type=click.Choice(list(SEASON_ES), case_sensitive=False), default=None)
@click.option("--ano", type=int, default=None)
@click.option("--descubrir", is_flag=True, help="Consultar AniList para la temporada indicada.")
@click.option("--limite", type=int, default=30, show_default=True)
@click.pass_obj
def cmd_airing(cfg: Config, temporada: str | None, ano: int | None, descubrir: bool, limite: int) -> None:
    """Muestra la biblioteca en emisión, la lista de seguimiento o la cartelera de AniList."""
    info = season_info()
    season = (temporada or info["season"]).upper()
    year = ano or info["year"]
    if descubrir:
        with console.status(f"Consultando AniList: {SEASON_ES.get(season, season)} {year}…"):
            items = discover_season(cfg, season, year)
            summary = annotate_season_downloads(cfg, items)
        table = _table(
            f"Cartelera {SEASON_ES.get(season, season)} {year}",
            [
                ("AniList", {"justify": "right"}),
                ("Título", {"style": "bold"}),
                ("Formato", {}),
                ("Eps", {"justify": "right"}),
                ("Nota", {"justify": "right"}),
                ("Próximo", {}),
                ("Descarga", {}),
            ],
        )
        for item in items[:limite]:
            next_text = (
                f"E{item['next_episode']} · {format_datetime_es(item.get('next_air_at'))}"
                if item.get("next_episode") else "—"
            )
            table.add_row(
                str(item["id"]), item["title"], item.get("format") or "—",
                str(item.get("episodes") or "?"), str(item.get("average_score") or "—"), next_text,
                item.get("download_label") or "—",
            )
        console.print(table)
        console.print(
            f"[dim]{summary['total']} títulos · {summary['in_library']} en biblioteca · "
            f"{summary['staged']} con descargas · {summary['followed']} en seguimiento.[/dim]"
        )
        console.print(
            "[dim]Añade uno con: python -m series_manager watch add <id> "
            "o varios con: python -m series_manager watch add-bulk <id> <id>…[/dim]"
        )
        return

    library = library_airing(cfg)
    watchlist = list_watch(cfg)
    table = _table("En emisión en la biblioteca", [
        ("Serie", {"style": "bold"}), ("Estado", {}), ("Última", {}), ("Próximo", {}), ("Biblioteca", {}),
    ])
    for item in library:
        next_text = f"E{item['next_episode']} · {item['next_air_label']}" if item.get("next_episode") else "—"
        table.add_row(item["title"], item["state"], item["last_aired_label"], next_text, item["root"])
    console.print(table)

    watch_table = _table("Lista de seguimiento", [
        ("Título", {"style": "bold"}), ("Año", {"justify": "right"}), ("Mi estado", {}),
        ("Visto", {"justify": "right"}), ("Próximo", {}),
    ])
    for item in watchlist:
        watch_table.add_row(
            item["title"], str(item.get("year") or "—"), item.get("watch_status") or "—",
            item["progress_label"],
            f"E{item['next_episode']}" if item.get("next_episode") else "—",
        )
    console.print(watch_table)

    upcoming_items = upcoming(cfg, 21)
    if upcoming_items:
        next_table = _table("Próximos 21 días", [("Cuándo", {}), ("Serie", {}), ("Episodio", {"justify": "right"})])
        for item in upcoming_items:
            next_table.add_row(format_datetime_es(item["when"]), item["title"], str(item.get("next_episode") or "—"))
        console.print(next_table)


@main.group("watch")
def cmd_watch() -> None:
    """Gestiona la lista de seguimiento de anime en emisión."""


@cmd_watch.command("list")
@click.pass_obj
def cmd_watch_list(cfg: Config) -> None:
    """Muestra la lista de seguimiento editable."""
    items = list_watch(cfg)
    table = _table("Lista de seguimiento", [
        ("ID", {"justify": "right"}), ("Título", {"style": "bold"}), ("Año", {"justify": "right"}),
        ("Mi estado", {}), ("Visto", {"justify": "right"}), ("Próximo", {}),
    ])
    for item in items:
        table.add_row(
            str(item["id"]),
            item["title"],
            str(item.get("year") or "—"),
            item.get("watch_status") or "—",
            item["progress_label"],
            format_datetime_es(item.get("next_air_at")),
        )
    console.print(table)


@cmd_watch.command("add")
@click.argument("anilist_id", type=int)
@click.pass_obj
def cmd_watch_add(cfg: Config, anilist_id: int) -> None:
    """Añade un anime de AniList por su ID."""
    with console.status("Consultando AniList…"):
        media = AniListClient(cfg).media(anilist_id)
    from .airing import add_watch

    add_watch(cfg, media)
    console.print(f"[green]Añadido:[/green] {media['title']} ({media.get('year') or '—'})")


@cmd_watch.command("add-bulk")
@click.argument("anilist_ids", nargs=-1, type=int, required=True)
@click.pass_obj
def cmd_watch_add_bulk(cfg: Config, anilist_ids: tuple[int, ...]) -> None:
    """Añade varios animes de AniList por sus IDs."""
    with console.status("Consultando AniList…"):
        result = follow_anilist_ids(cfg, list(anilist_ids))
    console.print(
        f"[green]Añadidos:[/green] {result['added']} · "
        f"actualizados: {result['updated']} · no encontrados: {result['missing']}"
    )
    if result["titles"]:
        console.print("[dim]" + " · ".join(result["titles"]) + "[/dim]")


@cmd_watch.command("add-manual")
@click.argument("titulo")
@click.option("--episodios", type=int, default=None, help="Total de episodios, si lo sabes.")
@click.option("--estado", type=click.Choice(WATCH_STATUSES), default="Viendo", show_default=True)
@click.option("--notas", default="", help="Notas personales.")
@click.pass_obj
def cmd_watch_add_manual(cfg: Config, titulo: str, episodios: int | None, estado: str, notas: str) -> None:
    """Añade un título manual a la lista de seguimiento."""
    watch_id = add_manual_watch(cfg, titulo, total_episodes=episodios, notes=notas, watch_status=estado)
    console.print(f"[green]Añadido manualmente:[/green] {titulo} (id {watch_id})")


@cmd_watch.command("edit")
@click.argument("watch_id", type=int)
@click.option("--estado", type=click.Choice(WATCH_STATUSES), default=None, help="Nuevo estado personal.")
@click.option("--visto", type=int, default=None, help="Episodio visto (progreso).")
@click.option("--nota", type=int, default=None, help="Nota personal de 0 a 10.")
@click.option("--notas", default=None, help="Notas personales.")
@click.pass_obj
def cmd_watch_edit(
    cfg: Config,
    watch_id: int,
    estado: str | None,
    visto: int | None,
    nota: int | None,
    notas: str | None,
) -> None:
    """Edita el estado, progreso, nota o notas de un título seguido."""
    update_watch(
        cfg,
        watch_id,
        watch_status=estado,
        progress_episode=visto,
        rating=nota,
        notes=notas,
    )
    console.print("[green]Seguimiento actualizado.[/green]")


@cmd_watch.command("advance")
@click.argument("watch_id", type=int)
@click.pass_obj
def cmd_watch_advance(cfg: Config, watch_id: int) -> None:
    """Marca el siguiente episodio como visto."""
    item = advance_watch(cfg, watch_id)
    progress = item.get("progress_episode")
    console.print(f"[green]Visto hasta el episodio {int(progress) if progress is not None else 0}:[/green] {item['title']}")


@cmd_watch.command("remove")
@click.argument("watch_id", type=int)
@click.pass_obj
def cmd_watch_remove(cfg: Config, watch_id: int) -> None:
    """Quita un título de la lista por su ID interno."""
    remove_watch(cfg, watch_id)
    console.print("[green]Eliminado de la lista de seguimiento.[/green]")


@cmd_watch.command("sync")
@click.pass_obj
def cmd_watch_sync(cfg: Config) -> None:
    """Actualiza los próximos episodios de la lista con AniList."""
    result = _run_with_progress(cfg, "watch", {}, "Actualizando seguimiento")
    console.print(f"Actualizados: [green]{result.get('updated', 0)}[/green] · errores: {result.get('errors', 0)}")


@main.command("jobs")
@click.option("--limite", type=int, default=20, show_default=True)
@click.pass_obj
def cmd_jobs(cfg: Config, limite: int) -> None:
    """Muestra el historial de trabajos."""
    table = _table("Trabajos", [
        ("ID", {}), ("Tipo", {}), ("Estado", {}), ("Progreso", {"justify": "right"}),
        ("Mensaje", {}), ("Creado", {}),
    ])
    for job in jobs.latest(cfg, limite):
        table.add_row(job["id"], job["kind"], job["status"], f"{job['progress']}%",
                      (job.get("message") or "")[:60], format_datetime_es(job.get("created_at")))
    console.print(table)


# ---------------------------------------------------------------------------
# Servidor web
# ---------------------------------------------------------------------------
@main.command("web")
@click.option("--host", default=None, help="Host de escucha (por defecto, el de config.json).")
@click.option("--puerto", type=int, default=None, help="Puerto (por defecto, el de config.json).")
@click.option("--sin-abrir", is_flag=True, help="No abrir el navegador.")
@click.pass_obj
def cmd_web(cfg: Config, host: str | None, puerto: int | None, sin_abrir: bool) -> None:
    """Inicia el panel web local."""
    import uvicorn

    web = cfg.get("web", {})
    host = host or web.get("host", "127.0.0.1")
    port = int(puerto or web.get("port", 5055))
    os.environ["SERIESADMIN_CONFIG"] = str(cfg.path)
    url = f"http://{host}:{port}"
    console.print(Panel.fit(
        f"Panel web: [cyan]{url}[/cyan]\n"
        f"Biblioteca: {cfg.library_root}\n"
        "Pulsa Ctrl+C para detener.",
        title="SeriesAdmin",
        border_style="cyan",
    ))
    if not sin_abrir:
        import threading

        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run("series_manager.web:app", host=host, port=port, log_level="info")


# ---------------------------------------------------------------------------
# Jellyfin y diagnóstico
# ---------------------------------------------------------------------------
@main.group("jellyfin")
def cmd_jellyfin() -> None:
    """Integración con Jellyfin."""


@cmd_jellyfin.command("test")
@click.pass_obj
def cmd_jellyfin_test(cfg: Config) -> None:
    """Comprueba la conexión con Jellyfin."""
    try:
        result = JellyfinClient(cfg).test()
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    console.print(
        f"[green]Jellyfin responde:[/green] {result['server']} {result['version']}\n"
        f"Usuarios: {', '.join(result['users']) or '—'}\n"
        f"Bibliotecas: " + ", ".join(
            f"{lib['name']} ({'; '.join(lib['locations'])})" for lib in result["libraries"]
        )
    )


@cmd_jellyfin.command("refresh")
@click.pass_obj
def cmd_jellyfin_refresh(cfg: Config) -> None:
    """Pide a Jellyfin que refresque las bibliotecas."""
    try:
        JellyfinClient(cfg).refresh_library()
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    console.print("[green]Jellyfin inició el refresco.[/green]")


@cmd_jellyfin.command("create-library")
@click.pass_obj
def cmd_jellyfin_create_library(cfg: Config) -> None:
    """Crea la biblioteca «Anime en emisión» en Jellyfin."""
    try:
        result = JellyfinClient(cfg).ensure_airing_library()
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    console.print(
        "[green]Biblioteca creada.[/green]" if result.get("created")
        else "[yellow]La biblioteca ya existía.[/yellow]"
    )


@cmd_jellyfin.command("backup")
@click.pass_obj
def cmd_jellyfin_backup(cfg: Config) -> None:
    """Copia el estado visto de Jellyfin a la base local."""
    try:
        result = backup_watch_state(cfg)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    console.print(f"Filas respaldadas: [green]{result['rows']}[/green] · usuarios: {result['users']}")


@main.command("doctor")
@click.option("--sin-red", is_flag=True, help="No probar TVmaze ni AniList.")
@click.pass_obj
def cmd_doctor(cfg: Config, sin_red: bool) -> None:
    """Diagnóstico del entorno, rutas, proveedores y Jellyfin."""
    checks: list[tuple[bool, str, str]] = []

    checks.append((sys.version_info >= (3, 10), "Python", platform.python_version()))
    checks.append((cfg.path.exists(), "config.json", str(cfg.path)))
    checks.append((cfg.db_path.exists(), "Base de datos", str(cfg.db_path)))
    checks.append((cfg.library_root.exists(), "Biblioteca", str(cfg.library_root)))
    checks.append((cfg.airing_root.exists(), "Biblioteca de emisión", str(cfg.airing_root)))
    ffmpeg = shutil.which("ffmpeg") or (r"C:\ffmpeg\bin\ffmpeg.exe" if Path(r"C:\ffmpeg\bin\ffmpeg.exe").exists() else None)
    checks.append((bool(ffmpeg), "ffmpeg", ffmpeg or "no encontrado (opcional)"))
    try:
        usage = shutil.disk_usage(cfg.library_root)
        checks.append((usage.free > 5 * 1024**3, "Espacio libre", human_size(usage.free)))
    except OSError:
        checks.append((False, "Espacio libre", "no se pudo leer"))

    if cfg.db_path.exists():
        with session(cfg.db_path) as con:
            series = con.execute("SELECT COUNT(*) FROM series").fetchone()[0]
            episodes = con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
            issues = con.execute("SELECT COUNT(*) FROM issues WHERE resolved=0").fetchone()[0]
            meta = {row["key"]: row["value"] for row in con.execute("SELECT key, value FROM meta")}
        checks.append((series > 0, "Catálogo", f"{series} series, {episodes} episodios, {issues} incidencias"))
        checks.append((bool(meta.get("last_scan")), "Último escaneo", format_datetime_es(meta.get("last_scan"))))
        checks.append((bool(meta.get("last_sync")), "Última sincronización", format_datetime_es(meta.get("last_sync"))))

    if not sin_red:
        try:
            shows = TVmazeClient(cfg).search("Cowboy Bebop", limit=1)
            checks.append((bool(shows), "TVmaze", shows[0]["title"] if shows else "sin resultados"))
        except Exception as exc:
            checks.append((False, "TVmaze", str(exc)))
        try:
            media = AniListClient(cfg).search("Cowboy Bebop", limit=1)
            checks.append((bool(media), "AniList", media[0]["title"] if media else "sin resultados"))
        except Exception as exc:
            checks.append((False, "AniList", str(exc)))

    api_key = (cfg.get("jellyfin", {}) or {}).get("api_key")
    if api_key:
        try:
            info = JellyfinClient(cfg).info()
            checks.append((True, "Jellyfin", f"{info.get('ServerName') or 'servidor'} {info.get('Version') or ''}"))
        except Exception as exc:
            checks.append((False, "Jellyfin", str(exc)))
    else:
        checks.append((False, "Jellyfin API", "sin clave API (opcional para previsualizar)"))

    table = _table("Diagnóstico", [("Estado", {}), ("Comprobación", {"style": "bold"}), ("Detalle", {})])
    for ok, label, detail in checks:
        table.add_row("[green]OK[/green]" if ok else "[yellow]AVISO[/yellow]", label, str(detail))
    console.print(table)


if __name__ == "__main__":
    main()
