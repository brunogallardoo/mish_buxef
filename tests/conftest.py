# tests/conftest.py - Harness compartido de la suite de pruebas de CampusRadar
"""
Configuración común para todos los archivos de test (pytest la carga
automáticamente):

1. Bcrypt/Passlib: se parchea CryptContext.hash/.verify con un hash SHA-256
   simulado para no depender del binario nativo de bcrypt en el entorno de test.
2. Base de datos: SQLite en memoria (StaticPool = una sola conexión compartida).
   La app accede a la BD por DOS rutas —el `SessionFactory` global del módulo
   `api` (escrituras vía UnitOfWork) y la dependencia `get_db` (lecturas)—, así
   que se redirigen AMBAS a la misma BD en memoria; de lo contrario el usuario
   registrado (escritura) no se encontraría al loguear (lectura).
"""

import hashlib
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.persistence import Base, crear_session_factory


def _fake_hash(self, password: str, *args, **kwargs) -> str:
    return "fakehash$" + hashlib.sha256(password.encode("utf-8")).hexdigest()


def _fake_verify(self, password: str, hashed: str, *args, **kwargs) -> bool:
    return hashed == _fake_hash(self, password)


# Parchear ANTES de importar la app (instancia CryptContext a nivel de módulo).
patch("passlib.context.CryptContext.hash", _fake_hash).start()
patch("passlib.context.CryptContext.verify", _fake_verify).start()

from app.api import app, get_db   # noqa: E402
import app.api as _api_mod         # noqa: E402

TEST_ENGINE = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionFactory = crear_session_factory(TEST_ENGINE)
_api_mod.SessionFactory = TestSessionFactory   # escrituras (UnitOfWork)


def _override_get_db():
    session = TestSessionFactory()
    try:
        yield session
    finally:
        session.close()


app.dependency_overrides[get_db] = _override_get_db   # lecturas (get_db)


@pytest.fixture(autouse=True)
def _reset_db():
    """Recrea el esquema antes de cada test para aislarlos entre sí."""
    Base.metadata.drop_all(TEST_ENGINE)
    Base.metadata.create_all(TEST_ENGINE)
    yield


@pytest.fixture
def client():
    return TestClient(app)
