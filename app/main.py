"""
CampusRadar - Entry Point
==========================

Punto de entrada unificado para uvicorn.
Carga la API REST (api.py) y la capa web Jinja2 (api_web.py)
sobre la misma instancia FastAPI.

Uso:
    uvicorn app.main:app --reload --port 8000
    python -m app.main               (modo desarrollo)
"""

# Importar primero la app base (API REST + configuración)
from app.api import app   # noqa: F401

# Luego importar la capa web, que monta rutas adicionales sobre `app`.
# Se usa `from app import api_web` (no `import app.api_web`) para NO rebindear
# el nombre `app` al paquete y pisar la instancia FastAPI importada arriba.
from app import api_web   # noqa: F401  (side-effect: registra /web/* y sobreescribe /)

from fastapi import FastAPI
app = FastAPI()  # Vercel buscará este objeto 'app' por defecto

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
