import reflex as rx
import os
from pathlib import Path
from reflex.plugins.sitemap import SitemapPlugin

# Запись данных не должна перезапускать сервер и обрывать загрузку/диалог.
_artifacts = Path(__file__).resolve().parent / "artifacts"
_artifacts.mkdir(exist_ok=True)
_excluded = os.environ.get("REFLEX_HOT_RELOAD_EXCLUDE_PATHS", "").split(os.pathsep)
os.environ["REFLEX_HOT_RELOAD_EXCLUDE_PATHS"] = os.pathsep.join(
    dict.fromkeys([p for p in _excluded if p] + [str(_artifacts)])
)

config = rx.Config(
    app_name="frontend",
    backend_host="127.0.0.1",
    disable_plugins=[SitemapPlugin],
    plugins=[rx.plugins.RadixThemesPlugin(theme=rx.theme(appearance="light", accent_color="bronze", radius="large"))],
)
