"""Serves the simulated Alexa page from the demo backend (same origin as /api/ask)."""
from pathlib import Path

from fastapi.responses import FileResponse

INDEX = Path(__file__).parent / "web" / "index.html"


def mount_web(app):
    async def _index():
        return FileResponse(INDEX, media_type="text/html", headers={"Cache-Control": "no-store"})

    app.add_api_route("/", _index, methods=["GET"], include_in_schema=False)
    app.add_api_route("/ui", _index, methods=["GET"], include_in_schema=False)
    return app
