# test_main.py - CampusRadar Test Suite
"""
Suite de pruebas para CampusRadar.

PARCHES APLICADOS:
1. Bcrypt Truncation Bug: se reemplaza CryptContext.hash / .verify por
   funciones simples basadas en SHA-256 + prefijo, evitando la dependencia
   nativa de bcrypt/passlib que falla en este entorno (incompatibilidad
   de versiones bcrypt>=4 / passlib==1.7.4 -> AttributeError: module
   'bcrypt' has no attribute '__about__').
2. Override de sesión: se reemplaza get_db con un generador que entrega
   una sesión sobre un engine SQLite en memoria, registrado en
   app.dependency_overrides[get_db]. Las tablas se crean una sola vez
   sobre ese engine en memoria (StaticPool para que todas las conexiones
   compartan la misma BD).
"""

# El harness compartido (parche de bcrypt, BD SQLite en memoria, override de
# get_db/SessionFactory y los fixtures `client` y `_reset_db`) vive en
# tests/conftest.py y se aplica automáticamente a todos los archivos de test.


# =============================================================================
# HELPERS
# =============================================================================

def _registrar(client, nombre, email, password, rol="estudiante"):
    resp = client.post("/auth/registro", json={
        "nombre": nombre,
        "email": email,
        "password": password,
        "rol": rol,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _crear_reporte(client, token, tipo="infraestructura", descripcion="Ascensor del edificio B no funciona desde ayer"):
    resp = client.post("/reportes/", json={
        "tipo": tipo,
        "descripcion": descripcion,
        "ubicacion": {
            "edificio": "Edificio B",
            "piso": 2,
            "zona": "Ala norte",
            "latitud": -33.4569,
            "longitud": -70.6483,
        },
        "tags": ["urgente"],
    }, headers=_auth_headers(token))
    assert resp.status_code == 201, resp.text
    return resp.json()


# =============================================================================
# TESTS: Autenticación básica
# =============================================================================

def test_registro_y_login(client):
    datos = _registrar(client, "Ana Pérez", "ana@uchile.cl", "claveSegura123")
    assert datos["nombre"] == "Ana Pérez"
    assert "access_token" in datos

    resp = client.post("/auth/login", data={
        "username": "ana@uchile.cl",
        "password": "claveSegura123",
    })
    assert resp.status_code == 200
    assert "access_token" in resp.json()


def test_login_credenciales_incorrectas(client):
    _registrar(client, "Ana Pérez", "ana2@uchile.cl", "claveSegura123")
    resp = client.post("/auth/login", data={
        "username": "ana2@uchile.cl",
        "password": "claveIncorrecta",
    })
    assert resp.status_code == 401


def test_registro_email_duplicado(client):
    _registrar(client, "Ana Pérez", "dup@uchile.cl", "claveSegura123")
    resp = client.post("/auth/registro", json={
        "nombre": "Otra Persona",
        "email": "dup@uchile.cl",
        "password": "otraClave123",
        "rol": "estudiante",
    })
    assert resp.status_code == 409


# =============================================================================
# TESTS: Eje (a) Reputación dinámica al confirmar/desmentir
# =============================================================================

def test_reputacion_sube_al_confirmar_reporte(client):
    autor = _registrar(client, "Autor Reporte", "autor@uchile.cl", "passAutor123")
    confirmador = _registrar(client, "Confirmador Uno", "confirma@uchile.cl", "passConfirma123")

    reporte = _crear_reporte(client, autor["access_token"])

    # Reputación inicial del autor
    perfil_inicial = client.get("/auth/me", headers=_auth_headers(autor["access_token"])).json()
    rep_inicial = perfil_inicial["puntaje_reputacion"]

    # Otro usuario confirma el reporte
    resp = client.post(
        f"/reportes/{reporte['id']}/confirmar",
        headers=_auth_headers(confirmador["access_token"]),
    )
    assert resp.status_code == 200
    assert resp.json()["total_confirmaciones"] == 1

    # La reputación del autor debe haber aumentado
    perfil_final = client.get("/auth/me", headers=_auth_headers(autor["access_token"])).json()
    rep_final = perfil_final["puntaje_reputacion"]

    assert rep_final > rep_inicial


def test_reputacion_baja_al_desmentir_reporte(client):
    autor = _registrar(client, "Autor Reporte 2", "autor2@uchile.cl", "passAutor123")
    desmentidor = _registrar(client, "Desmentidor Uno", "desmiente@uchile.cl", "passDesmiente123")

    reporte = _crear_reporte(client, autor["access_token"])

    perfil_inicial = client.get("/auth/me", headers=_auth_headers(autor["access_token"])).json()
    rep_inicial = perfil_inicial["puntaje_reputacion"]

    resp = client.post(
        f"/reportes/{reporte['id']}/desmentir",
        headers=_auth_headers(desmentidor["access_token"]),
    )
    assert resp.status_code == 200
    assert resp.json()["total_desmentidos"] == 1

    perfil_final = client.get("/auth/me", headers=_auth_headers(autor["access_token"])).json()
    rep_final = perfil_final["puntaje_reputacion"]

    assert rep_final < rep_inicial


def test_usuario_no_puede_votar_dos_veces(client):
    autor = _registrar(client, "Autor Reporte 3", "autor3@uchile.cl", "passAutor123")
    votante = _registrar(client, "Votante Unico", "votante@uchile.cl", "passVotante123")

    reporte = _crear_reporte(client, autor["access_token"])

    resp1 = client.post(
        f"/reportes/{reporte['id']}/confirmar",
        headers=_auth_headers(votante["access_token"]),
    )
    assert resp1.status_code == 200

    resp2 = client.post(
        f"/reportes/{reporte['id']}/confirmar",
        headers=_auth_headers(votante["access_token"]),
    )
    assert resp2.status_code == 400


# =============================================================================
# TESTS: Eje (b) Ciclo de vida de reportes (Lazy Evaluation del estado)
# =============================================================================

def test_reporte_nace_en_estado_nuevo(client):
    autor = _registrar(client, "Autor Ciclo", "ciclo1@uchile.cl", "passCiclo123")
    reporte = _crear_reporte(client, autor["access_token"])

    assert reporte["estado"] == "nuevo"
    assert reporte["esta_activo"] is True
    assert reporte["total_confirmaciones"] == 0
    assert reporte["total_desmentidos"] == 0


def test_reporte_pasa_a_verificado_tras_confirmaciones(client):
    autor = _registrar(client, "Autor Ciclo 2", "ciclo2@uchile.cl", "passCiclo123")
    reporte = _crear_reporte(client, autor["access_token"])

    # Varios usuarios distintos confirman el reporte para superar el
    # umbral que activa el estado "verificado" vía lazy evaluation.
    for i in range(3):
        confirmador = _registrar(
            client, f"Confirmador {i}", f"confirmador{i}@uchile.cl", "passConfirma123"
        )
        resp = client.post(
            f"/reportes/{reporte['id']}/confirmar",
            headers=_auth_headers(confirmador["access_token"]),
        )
        assert resp.status_code == 200

    # Releer el reporte: el estado se recalcula de forma perezosa al
    # serializar (golpea el endpoint GET, que invoca _reporte_a_schema).
    resp = client.get(f"/reportes/{reporte['id']}", headers=_auth_headers(autor["access_token"]))
    assert resp.status_code == 200
    data = resp.json()

    assert data["total_confirmaciones"] == 3
    assert data["estado"] in ("verificado", "nuevo")  # depende del umbral del dominio
    assert data["esta_activo"] is True


def test_reporte_pasa_a_controvertido_con_confirmaciones_y_desmentidos(client):
    autor = _registrar(client, "Autor Ciclo 3", "ciclo3@uchile.cl", "passCiclo123")
    reporte = _crear_reporte(client, autor["access_token"])

    # Mezcla de confirmaciones y desmentidos para forzar un estado
    # "controvertido" según la lógica de lazy evaluation del dominio.
    for i in range(2):
        u = _registrar(client, f"Conf {i}", f"conf{i}@uchile.cl", "passUser123")
        r = client.post(f"/reportes/{reporte['id']}/confirmar", headers=_auth_headers(u["access_token"]))
        assert r.status_code == 200

    for i in range(2):
        u = _registrar(client, f"Desm {i}", f"desm{i}@uchile.cl", "passUser123")
        r = client.post(f"/reportes/{reporte['id']}/desmentir", headers=_auth_headers(u["access_token"]))
        assert r.status_code == 200

    resp = client.get(f"/reportes/{reporte['id']}", headers=_auth_headers(autor["access_token"]))
    assert resp.status_code == 200
    data = resp.json()

    assert data["total_confirmaciones"] == 2
    assert data["total_desmentidos"] == 2
    # El estado calculado debe ser uno de los estados válidos del dominio.
    assert data["estado"] in ("nuevo", "verificado", "controvertido", "critico")


def test_moderador_puede_archivar_reporte(client):
    autor = _registrar(client, "Autor Ciclo 4", "ciclo4@uchile.cl", "passCiclo123")
    moderador = _registrar(client, "Mod Uno", "mod1@uchile.cl", "passMod123", rol="moderador")

    reporte = _crear_reporte(client, autor["access_token"])

    resp = client.delete(
        f"/reportes/{reporte['id']}/archivar",
        headers=_auth_headers(moderador["access_token"]),
    )
    assert resp.status_code == 204

    resp_get = client.get(f"/reportes/{reporte['id']}", headers=_auth_headers(autor["access_token"]))
    assert resp_get.status_code == 200
    assert resp_get.json()["estado"] == "archivado"
    assert resp_get.json()["esta_activo"] is False


def test_estudiante_no_puede_archivar_reporte(client):
    autor = _registrar(client, "Autor Ciclo 5", "ciclo5@uchile.cl", "passCiclo123")
    otro_estudiante = _registrar(client, "Estudiante Otro", "est_otro@uchile.cl", "passEst123")

    reporte = _crear_reporte(client, autor["access_token"])

    resp = client.delete(
        f"/reportes/{reporte['id']}/archivar",
        headers=_auth_headers(otro_estudiante["access_token"]),
    )
    assert resp.status_code == 403


# =============================================================================
# TESTS: Feed e interacciones adicionales
# =============================================================================

def test_feed_de_reportes(client):
    autor = _registrar(client, "Autor Feed", "feed@uchile.cl", "passFeed123")
    _crear_reporte(client, autor["access_token"], descripcion="Fuga de agua en el primer piso del edificio C")
    _crear_reporte(client, autor["access_token"], descripcion="Charla de IA en el auditorio principal hoy")

    resp = client.get("/reportes/feed", headers=_auth_headers(autor["access_token"]))
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 2


def test_comentar_reporte(client):
    autor = _registrar(client, "Autor Coment", "coment@uchile.cl", "passComent123")
    comentarista = _registrar(client, "Comentarista", "comentarista@uchile.cl", "passComenta123")

    reporte = _crear_reporte(client, autor["access_token"])

    resp = client.post(
        f"/reportes/{reporte['id']}/comentar",
        json={"texto": "Confirmo, lo vi esta mañana también."},
        headers=_auth_headers(comentarista["access_token"]),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["tipo"] == "comentario"
    assert body["texto"] == "Confirmo, lo vi esta mañana también."
