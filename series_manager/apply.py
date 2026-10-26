"""Aplicación y deshacer de planes con diario de operaciones."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .db import session
from .naming import clean_empty_dirs
from .utils import ensure_parent, is_subpath, now_iso


class PlanError(RuntimeError):
    pass


def _journal(con, plan_id: int, action: str, source: str, target: str, status: str, error: str | None = None, meta: str | None = None) -> int:
    con.execute(
        "INSERT INTO journal(plan_id, applied_at, action, source, target, status, error, undo_status, meta) "
        "VALUES(?,?,?,?,?,?,?,'pending',?)",
        (plan_id, now_iso(), action, source, target, status, error, meta),
    )
    return int(con.execute("SELECT last_insert_rowid()").fetchone()[0])


def _validate_paths(cfg: Config, source: Path, target: Path) -> None:
    allowed_targets = [cfg.library_root, cfg.airing_root, cfg.quarantine_root]
    if not any(is_subpath(target, root) for root in allowed_targets):
        raise PlanError(f"Destino fuera de las carpetas permitidas: {target}")
    if not any(is_subpath(source, root) for root in [cfg.library_root, cfg.airing_root, cfg.quarantine_root]):
        raise PlanError(f"Origen fuera de las carpetas permitidas: {source}")


def _move_file(source: Path, target: Path) -> None:
    if not source.exists():
        raise PlanError(f"No existe el origen: {source}")
    if target.exists():
        raise PlanError(f"El destino ya existe: {target}")
    source_size = source.stat().st_size
    ensure_parent(target)
    shutil.move(str(source), str(target))
    if not target.exists():
        raise PlanError(f"La operación no creó el destino: {target}")
    if target.stat().st_size != source_size:
        raise PlanError(f"El tamaño no coincide tras mover: {target}")


def _update_file_path(con, file_id: int | None, new_path: Path, status: str, reason: str | None = None) -> None:
    if not file_id:
        return
    con.execute(
        "UPDATE files SET path=?, status=?, reason=?, updated_at=? WHERE id=?",
        (str(new_path), status, reason, now_iso(), int(file_id)),
    )


def apply_plan(
    cfg: Config,
    plan_id: int,
    progress: Callable[[int, str], None] | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    cfg.ensure_dirs()
    stats = {"done": 0, "skipped": 0, "errors": 0, "bytes": 0, "messages": []}
    with session(cfg.db_path) as con:
        plan = con.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not plan:
            raise PlanError(f"No existe el plan {plan_id}")
        if plan["status"] not in {"ready", "draft", "partial"}:
            raise PlanError(f"El plan está en estado {plan['status']} y no se puede aplicar")
        actions = con.execute(
            "SELECT * FROM plan_actions WHERE plan_id=? ORDER BY seq", (plan_id,)
        ).fetchall()
        total = max(1, len(actions))
        if not dry_run:
            con.execute("UPDATE plans SET status='applying' WHERE id=?", (plan_id,))

        for index, action in enumerate(actions, start=1):
            if progress:
                progress(int((index - 1) / total * 100), f"Aplicando {action['action']}")
            if action["status"] not in {"pending", "error"}:
                stats["skipped"] += 1
                continue
            if action["action"] == "conflict":
                con.execute(
                    "UPDATE plan_actions SET status='skipped', error=? WHERE id=?",
                    ("Conflicto no aplicado; requiere revisión", action["id"]),
                )
                stats["skipped"] += 1
                continue
            if action["action"] not in {"move", "rename", "quarantine"}:
                con.execute("UPDATE plan_actions SET status='skipped' WHERE id=?", (action["id"],))
                stats["skipped"] += 1
                continue
            source = Path(action["source"] or "")
            target = Path(action["target"] or "")
            try:
                _validate_paths(cfg, source, target)
                if dry_run:
                    if not source.exists():
                        raise PlanError(f"Falta el origen: {source}")
                    if target.exists():
                        raise PlanError(f"El destino ya existe: {target}")
                    stats["done"] += 1
                    continue
                journal_id = _journal(
                    con, plan_id, action["action"], str(source), str(target), "running",
                    meta=action["meta"],
                )
                _move_file(source, target)
                con.execute(
                    "UPDATE journal SET status='done', applied_at=? WHERE id=?",
                    (now_iso(), journal_id),
                )
                new_status = "quarantined" if action["action"] == "quarantine" else "present"
                _update_file_path(
                    con, action["file_id"], target, new_status,
                    action["reason"] if action["action"] == "quarantine" else None,
                )
                con.execute(
                    "UPDATE plan_actions SET status='done' WHERE id=?", (action["id"],)
                )
                con.commit()
                stats["done"] += 1
                stats["bytes"] += int(action["size"] or 0)
            except Exception as exc:
                stats["errors"] += 1
                stats["messages"].append(str(exc))
                con.execute(
                    "UPDATE plan_actions SET status='error', error=? WHERE id=?",
                    (str(exc), action["id"]),
                )
                if not dry_run:
                    _journal(
                        con, plan_id, action["action"], str(source), str(target),
                        "error", str(exc), meta=action["meta"],
                    )
                con.commit()

        if not dry_run:
            status = "applied" if stats["errors"] == 0 else "partial"
            con.execute(
                "UPDATE plans SET status=?, applied_at=? WHERE id=?",
                (status, now_iso(), plan_id),
            )
            # Limpiar solo carpetas vacías; nunca archivos.
            try:
                clean_empty_dirs(cfg.library_root)
                if cfg.airing_root.exists():
                    clean_empty_dirs(cfg.airing_root)
            except OSError:
                pass
    if progress:
        progress(100, "Aplicación terminada")
    return stats


def undo_plan(
    cfg: Config,
    plan_id: int,
    progress: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    stats = {"done": 0, "errors": 0, "skipped": 0, "messages": []}
    with session(cfg.db_path) as con:
        plan = con.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not plan:
            raise PlanError(f"No existe el plan {plan_id}")
        entries = con.execute(
            "SELECT * FROM journal WHERE plan_id=? AND status='done' AND undo_status='pending' "
            "ORDER BY id DESC",
            (plan_id,),
        ).fetchall()
        total = max(1, len(entries))
        for index, entry in enumerate(entries, start=1):
            if progress:
                progress(int((index - 1) / total * 100), "Deshaciendo")
            source = Path(entry["source"] or "")
            target = Path(entry["target"] or "")
            try:
                _validate_paths(cfg, source, target)
                if not target.exists():
                    raise PlanError(f"Falta el archivo movido: {target}")
                if source.exists():
                    raise PlanError(f"El origen ya existe: {source}")
                ensure_parent(source)
                shutil.move(str(target), str(source))
                con.execute(
                    "UPDATE journal SET undo_status='done', undone_at=? WHERE id=?",
                    (now_iso(), entry["id"]),
                )
                # Actualizar la ruta del archivo por coincidencia de ruta actual.
                con.execute(
                    "UPDATE files SET path=?, status='present', reason=NULL, updated_at=? WHERE path=?",
                    (str(source), now_iso(), str(target)),
                )
                con.commit()
                stats["done"] += 1
            except Exception as exc:
                stats["errors"] += 1
                stats["messages"].append(str(exc))
                con.execute(
                    "UPDATE journal SET undo_status='error', error=? WHERE id=?",
                    (str(exc), entry["id"]),
                )
                con.commit()
        con.execute(
            "UPDATE plans SET status='undone', undone_at=? WHERE id=?",
            (now_iso(), plan_id),
        )
    if progress:
        progress(100, "Deshacer terminado")
    return stats


def plan_actions(con, plan_id: int):
    return con.execute(
        "SELECT * FROM plan_actions WHERE plan_id=? ORDER BY seq", (plan_id,)
    ).fetchall()
