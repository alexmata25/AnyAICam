from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse

ROOT=Path(__file__).with_name('pwa')


def register_pwa_routes(app: FastAPI):
    @app.get('/manifest.webmanifest',include_in_schema=False)
    def pwa_manifest(): return FileResponse(ROOT/'manifest.webmanifest',media_type='application/manifest+json',headers={'Cache-Control':'public, max-age=3600'})

    @app.get('/service-worker.js',include_in_schema=False)
    def pwa_service_worker(): return FileResponse(ROOT/'service-worker.js',media_type='application/javascript',headers={'Cache-Control':'no-cache','Service-Worker-Allowed':'/'})

    @app.get('/pwa-offline',include_in_schema=False)
    def pwa_offline(): return FileResponse(ROOT/'offline.html',media_type='text/html',headers={'Cache-Control':'public, max-age=86400'})
