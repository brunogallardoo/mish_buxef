"""
CampusRadar - Configuración centralizada
=========================================

Toda la configuración del sistema (clave de firma JWT, parámetros del token y
URL de la base de datos) se lee desde variables de entorno y, opcionalmente,
desde un archivo `.env` en la raíz del proyecto.

Se usa `pydantic-settings` (BaseSettings) en lugar de `os.getenv` disperso para:
  - tener un único punto de verdad y tipado/validación de la config,
  - cargar automáticamente un `.env` en desarrollo (sin versionarlo), y
  - permitir sobreescribir cualquier valor por variable de entorno en despliegue.

Los nombres de variables de entorno son insensibles a mayúsculas, por lo que
`SECRET_KEY`, `DATABASE_URL`, etc. mapean a los campos de abajo.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # seguridad
    secret_key: str = "campusradar-dev-secret-key-cambiar-en-produccion"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 8  # 8 horas

    # --- Base de datos ---
    # Puede ser una URL completa de SQLAlchemy
    # (p.ej. 'postgresql+psycopg2://user:pass@host:5432/campusradar' o
    # 'sqlite:////app/data/campusradar.db') o una ruta de archivo simple, que se
    # interpreta como una base SQLite local (ver `crear_engine` en persistence.py).
    database_url: str = "campusradar.db"


# Instancia única importada por el resto de la aplicación.
settings = Settings()
