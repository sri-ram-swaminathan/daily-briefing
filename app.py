"""The Daily Briefing / News Council server: the website, the live voice briefing and the APIs.

One process, one port:

    /                      the website (site/), in live mode against this server
    /voice?briefing=<id>   the interruptible listening page (web/)
    /api/...               research desk, council, briefings (voice_agent/server.py)
    /ws/session            the live audio websocket

    uv run app.py          # http://localhost:7860

On first start the news data is synced from the private Hugging Face bucket in HF_DATA_URL
(data_store.py). Without it the website and voice still run; the research desk says it has no articles.
"""

import os

from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from voice_agent import server as voice

app = voice.app


@app.get("/classic", include_in_schema=False)
async def classic():  # the retired Gradio UI used to live here
    return RedirectResponse("/#briefing")


if voice.settings.site_dir.is_dir():
    # Mounted last so /api, /voice and /ws win.
    app.mount("/", StaticFiles(directory=voice.settings.site_dir, html=True), name="site")


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST") or os.getenv("GRADIO_SERVER_NAME", "127.0.0.1")
    port = int(os.getenv("PORT") or os.getenv("GRADIO_SERVER_PORT", "7860"))
    print(f"Daily Briefing running on http://{'localhost' if host in {'127.0.0.1', '0.0.0.0'} else host}:{port}")
    uvicorn.run(app, host=host, port=port)
