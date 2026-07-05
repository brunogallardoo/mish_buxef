# tests/test_notificaciones.py - Suscripciones y notificaciones
"""
Pruebas de la funcionalidad de seguir tags/edificios y recibir notificaciones.
Incluye un test de dominio puro (regla de coincidencia) y tests de integración
HTTP. El harness (BD en memoria, parche de bcrypt) vive en tests/conftest.py.
"""

from app.domain import (
    Suscripcion, TipoSuscripcion, Estudiante,
    ReporteInfraestructura, Ubicacion,
)


# --- Helpers locales ---

def _registrar(client, nombre, email, password, rol="estudiante"):
    r = client.post("/auth/registro", json={
        "nombre": nombre, "email": email, "password": password, "rol": rol,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _H(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _crear_reporte(client, token, edificio="Edificio B", tags=("urgente",)):
    r = client.post("/reportes/", json={
        "tipo": "infraestructura",
        "descripcion": "Ascensor del edificio detenido desde la mañana",
        "ubicacion": {"edificio": edificio, "piso": 1},
        "tags": list(tags),
    }, headers=_H(token))
    assert r.status_code == 201, r.text
    return r.json()


# =============================================================================
# Dominio puro: regla de coincidencia de suscripciones
# =============================================================================

def test_suscripcion_coincide_por_tag_y_edificio():
    autor = Estudiante(nombre="A", email="a@uchile.cl", password_hash="h")
    reporte = ReporteInfraestructura(
        autor=autor,
        descripcion="Microondas roto en la cocina del piso 2",
        ubicacion=Ubicacion(edificio="Hall Sur", piso=2),
        tags={"microondas", "cocina"},
    )
    assert Suscripcion(usuario_id=9, tipo=TipoSuscripcion.TAG, valor="microondas").coincide(reporte) is True
    assert Suscripcion(usuario_id=9, tipo=TipoSuscripcion.TAG, valor="ascensor").coincide(reporte) is False
    # case-insensitive en edificio
    assert Suscripcion(usuario_id=9, tipo=TipoSuscripcion.EDIFICIO, valor="hall sur").coincide(reporte) is True
    assert Suscripcion(usuario_id=9, tipo=TipoSuscripcion.EDIFICIO, valor="biblioteca").coincide(reporte) is False


# =============================================================================
# Integración HTTP
# =============================================================================

def test_notificacion_por_tag_seguido(client):
    seguidor = _registrar(client, "Seguidor", "seg@uchile.cl", "clave123")
    autor = _registrar(client, "Autor", "aut@uchile.cl", "clave123")

    r = client.post("/suscripciones/", json={"tipo": "tag", "valor": "urgente"},
                    headers=_H(seguidor["access_token"]))
    assert r.status_code == 201

    _crear_reporte(client, autor["access_token"], tags=("urgente",))

    notifs = client.get("/notificaciones/", headers=_H(seguidor["access_token"])).json()
    assert len(notifs) == 1
    assert notifs[0]["leida"] is False
    assert "urgente" in notifs[0]["mensaje"]

    conteo = client.get("/notificaciones/conteo", headers=_H(seguidor["access_token"])).json()
    assert conteo["no_leidas"] == 1

    nid = notifs[0]["id"]
    assert client.post(f"/notificaciones/{nid}/leer", headers=_H(seguidor["access_token"])).status_code == 204
    assert client.get("/notificaciones/conteo", headers=_H(seguidor["access_token"])).json()["no_leidas"] == 0


def test_notificacion_por_edificio_seguido(client):
    seguidor = _registrar(client, "Seg2", "seg2@uchile.cl", "clave123")
    autor = _registrar(client, "Aut2", "aut2@uchile.cl", "clave123")

    client.post("/suscripciones/", json={"tipo": "edificio", "valor": "Edificio B"},
                headers=_H(seguidor["access_token"]))
    _crear_reporte(client, autor["access_token"], edificio="Edificio B", tags=("otro",))

    notifs = client.get("/notificaciones/", headers=_H(seguidor["access_token"])).json()
    assert len(notifs) == 1


def test_autor_no_se_notifica_a_si_mismo(client):
    autor = _registrar(client, "AutoSeg", "auto@uchile.cl", "clave123")
    client.post("/suscripciones/", json={"tipo": "tag", "valor": "urgente"},
                headers=_H(autor["access_token"]))
    _crear_reporte(client, autor["access_token"], tags=("urgente",))

    notifs = client.get("/notificaciones/", headers=_H(autor["access_token"])).json()
    assert notifs == []


def test_suscripcion_crud_e_idempotencia(client):
    u = _registrar(client, "Cruder", "crud@uchile.cl", "clave123")
    h = _H(u["access_token"])

    assert client.post("/suscripciones/", json={"tipo": "tag", "valor": "Urgente"}, headers=h).status_code == 201
    # idempotente: misma suscripción (normalizada a minúsculas) no se duplica
    assert client.post("/suscripciones/", json={"tipo": "tag", "valor": "urgente"}, headers=h).status_code == 201

    subs = client.get("/suscripciones/", headers=h).json()
    assert len(subs) == 1
    assert subs[0]["valor"] == "urgente"

    sid = subs[0]["id"]
    assert client.delete(f"/suscripciones/{sid}", headers=h).status_code == 204
    assert client.get("/suscripciones/", headers=h).json() == []


def test_tipo_suscripcion_invalido(client):
    u = _registrar(client, "Inval", "inval@uchile.cl", "clave123")
    r = client.post("/suscripciones/", json={"tipo": "zona", "valor": "x"}, headers=_H(u["access_token"]))
    assert r.status_code == 422
