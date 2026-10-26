"""Informes HTML del plan y del catálogo."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import Config
from .db import session
from .utils import format_date_es, human_size, json_loads


def jinja_env(cfg: Config) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(cfg.app_root / "templates")),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["human_size"] = human_size
    env.filters["date_es"] = format_date_es
    env.globals["app_name"] = "SeriesAdmin"
    return env


def write_plan_report(cfg: Config, plan_id: int) -> Path:
    cfg.reports_root.mkdir(parents=True, exist_ok=True)
    with session(cfg.db_path) as con:
        plan = con.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not plan:
            raise ValueError(f"No existe el plan {plan_id}")
        actions = con.execute(
            "SELECT a.*, s.title AS series_title, s.year AS series_year "
            "FROM plan_actions a LEFT JOIN series s ON s.id=a.series_id "
            "WHERE a.plan_id=? ORDER BY a.seq",
            (plan_id,),
        ).fetchall()
        summary = json_loads(plan["summary"], {}) or {}
        series_count = con.execute("SELECT COUNT(*) FROM series").fetchone()[0]
        episode_count = con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        file_count = con.execute("SELECT COUNT(*) FROM files WHERE kind='video'").fetchone()[0]
        issues = con.execute(
            "SELECT * FROM issues WHERE resolved=0 ORDER BY severity DESC, id DESC LIMIT 60"
        ).fetchall()
    env = jinja_env(cfg)
    template = env.get_template("plan_report.html")
    html = template.render(
        plan=dict(plan),
        actions=[dict(a) for a in actions],
        summary=summary,
        series_count=series_count,
        episode_count=episode_count,
        file_count=file_count,
        issues=[dict(i) for i in issues],
        generated_at=__import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    path = cfg.reports_root / f"plan_{plan_id}.html"
    path.write_text(html, encoding="utf-8")
    return path
