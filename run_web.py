"""Arranque directo del panel web (alternativa a `python -m series_manager web`)."""
from __future__ import annotations

import os
import threading
import webbrowser

import uvicorn

from series_manager.config import Config


def main() -> None:
    cfg = Config()
    cfg.ensure_dirs()
    os.environ["SERIESADMIN_CONFIG"] = str(cfg.path)
    web = cfg.get("web", {}) or {}
    host = str(web.get("host", "127.0.0.1"))
    port = int(web.get("port", 5055))
    url = f"http://{host}:{port}"
    print(f"SeriesAdmin · panel web: {url}")
    print(f"Biblioteca: {cfg.library_root}")
    if web.get("open_browser", True):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run("series_manager.web:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
