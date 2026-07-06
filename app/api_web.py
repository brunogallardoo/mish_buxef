"""
CampusRadar - Capa Web (Jinja2 + HTML)

Aquí estan los endpoints para el HTML, usamos Jinja2 y API REST (api.py)


Se importa y monta sobre la misma instancia `app` de FastAPI para que
convivan en el mismo proceso: las rutas /api/* siguen siendo JSON REST,
mientras que / y /web/* devuelven HTML renderizado.

IMPORTANTE:

- usamos jwt almacenada en cookies http only. Las peticiones ajax del frontend usan el header bearer token.
- el frontend usa tailwind css y leaflet para el mapa.
- evitamos duplicacion de lógica de negocio usando la vista de datos del api rest
- si no hay usuario se usa el usuario demo

DECISIONES DE DISEÑO:
- Se usa la misma autenticación JWT pero almacenada en cookies HTTP-only
  (en lugar de Authorization header), lo cual es más natural para
  navegadores web. Las peticiones AJAX del frontend siguen usando el
  header Bearer Token (LocalStorage → fetch con Authorization).
- El frontend usa Tailwind CSS vía CDN y Leaflet.js para el mapa, sin
  necesidad de un build step (npm, webpack, etc.), manteniendo la
  simplicidad que pide el enunciado.
- Las vistas leen datos de los mismos repositorios que la API REST,
  garantizando consistencia sin duplicar lógica de negocio.
- Se crea un "usuario demo" automáticamente si no existe ningún usuario
  en la BD, facilitando el primer uso sin registro manual.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Optional, Annotated

from fastapi import (
    APIRouter, Depends, Form, HTTPException, Request, Response,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from sqlalchemy.orm import Session

# capas internas
from app.domain import (
    Estudiante, Moderador, Administrador,
    ReporteInfraestructura, ReporteActividadExtraprogramatica,
    ReporteAlertaEmergencia, ReporteEventoUniversitario, ReporteInformacionLogistica,
    Ubicacion, EstadoReporte, Permiso, TipoSuscripcion,
)
from app.persistence import RolUsuario
from app.repositories import UnitOfWork, EmailDuplicado

# Importamos desde api.py lo que ya está definido
from app.api import (
    app, SessionFactory,
    hashear_password, verificar_password, crear_access_token,
    _reporte_a_schema, _usuario_a_schema,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    SECRET_KEY, ALGORITHM,
    TIPOS_REPORTE_VALIDOS, _TIPO_A_CLASE_DOMINIO,
)
from jose import JWTError, jwt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Servir archivos estáticos (CSS custom, íconos, etc.) si existen
static_dir = os.path.join(BASE_DIR, "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


def _get_usuario_desde_cookie(
    request: Request,
    db: Session,
    campusradar_token: Optional[str] = None,
) -> Optional[object]:
    token = campusradar_token or request.cookies.get("campusradar_token")
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email = payload.get("sub")
        if not email:
            return None
    except JWTError:
        return None

    from app.repositories import UsuarioRepository
    repo = UsuarioRepository(db)
    usuario = repo.obtener_por_email(email)
    return usuario if (usuario and usuario.activo) else None


def _require_usuario(request: Request, db: Session) -> object:
    """Como _get_usuario_desde_cookie pero redirige al login si no hay sesión."""
    usuario = _get_usuario_desde_cookie(request, db)
    if usuario is None:
        raise HTTPException(
            status_code=302,
            headers={"Location": "/web/login"},
        )
    return usuario


def _get_db_web():
    session = SessionFactory()
    try:
        yield session
    finally:
        session.close()


def _seed_demo_user(db: Session) -> None:
    """ ¡¡¡¡IMPORTANTE!!!!!
    Crea usuarios demo si la BD está vacía. Facilita el primer uso.
    Credenciales: admin@campus.cl / admin123 (administrador)
                  estudiante@campus.cl / est123 (estudiante)
    """
    from sqlalchemy import select
    from app.persistence import UsuarioORM
    count = db.scalar(select(UsuarioORM).limit(1))
    if count is not None:
        return  # ya hay usuarios

    admin = Administrador(
        nombre="Admin CampusRadar",
        email="admin@campus.cl",
        password_hash=hashear_password("admin123"),
    )
    est = Estudiante(
        nombre="Estudiante Demo",
        email="estudiante@campus.cl",
        password_hash=hashear_password("est123"),
    )
    with UnitOfWork(SessionFactory) as uow:
        try:
            uow.usuarios.guardar(admin)
            uow.usuarios.guardar(est)
            uow.commit()
        except Exception:
            uow.rollback()

# router web

router_web = APIRouter(prefix="/web", tags=["Web UI"])


# login
@router_web.get("/login", response_class=HTMLResponse)
def web_login_form(request: Request, error: Optional[str] = None):
    """Muestra el formulario de login."""
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": error,
    })


@router_web.post("/login")
def web_login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    """Procesa el login: valida credenciales y setea cookie JWT."""
    session = SessionFactory()
    try:
        from app.repositories import UsuarioRepository
        repo = UsuarioRepository(session)
        usuario = repo.obtener_por_email(email)
    finally:
        session.close()

    if usuario is None or not verificar_password(password, usuario._password_hash):
        return RedirectResponse(
            url="/web/login?error=Credenciales incorrectas",
            status_code=302,
        )
    if not usuario.activo:
        return RedirectResponse(
            url="/web/login?error=Cuenta desactivada",
            status_code=302,
        )

    rol_map = {
        Estudiante: RolUsuario.ESTUDIANTE,
        Moderador: RolUsuario.MODERADOR,
        Administrador: RolUsuario.ADMINISTRADOR,
    }
    rol = rol_map.get(type(usuario), RolUsuario.ESTUDIANTE)
    token = crear_access_token(
        {"sub": usuario.email, "rol": rol},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )

    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key="campusradar_token",
        value=token,
        httponly=True,
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        samesite="lax",
    )
    return response


@router_web.get("/logout")
def web_logout():
    """Cierra sesión eliminando la cookie."""
    response = RedirectResponse(url="/web/login", status_code=302)
    response.delete_cookie("campusradar_token")
    return response


# registro
@router_web.get("/registro", response_class=HTMLResponse)
def web_registro_form(request: Request, error: Optional[str] = None):
    return templates.TemplateResponse("registro.html", {
        "request": request, "error": error,
    })


@router_web.post("/registro")
def web_registro_submit(
    nombre: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    rol: str = Form(default="estudiante"),
):
    clase_map = {
        "estudiante": Estudiante,
        "moderador": Moderador,
        "administrador": Administrador,
    }
    clase = clase_map.get(rol, Estudiante)
    usuario = clase(
        nombre=nombre,
        email=email,
        password_hash=hashear_password(password),
    )
    try:
        with UnitOfWork(SessionFactory) as uow:
            uow.usuarios.guardar(usuario)
            uow.commit()
    except EmailDuplicado:
        return RedirectResponse(
            url="/web/registro?error=Email ya registrado",
            status_code=302,
        )

    return RedirectResponse(url="/web/login", status_code=302)


# index
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """
    Página principal. Si no hay sesión, redirige al login.
    Si hay sesión, renderiza el dashboard completo con:
    - Panel del usuario actual
    - Feed de reportes activos con sus estados
    - Formulario de creación de nuevo reporte
    - Mapa Leaflet con reportes geolocalizados
    """
    session = SessionFactory()
    try:
        _seed_demo_user(session)  # crear usuarios demo si BD vacía
        usuario = _get_usuario_desde_cookie(request, session)
        if usuario is None:
            return RedirectResponse(url="/web/login", status_code=302)

        from app.repositories import ReporteRepository
        repo = ReporteRepository(session)
        reportes_dominio = repo.feed_ordenado(limite=30)
        reportes = [_reporte_a_schema(r, usuario_id=usuario.id) for r in reportes_dominio]
        usuario_schema = _usuario_a_schema(usuario)

        # Mapear colores y etiquetas
        estado_config = {
            "nuevo":         {"color": "blue",   "label": "Nuevo",         "badge": "bg-blue-100 text-blue-800"},
            "verificado":    {"color": "green",  "label": "Verificado",    "badge": "bg-green-100 text-green-800"},
            "controvertido": {"color": "yellow", "label": "Controvertido", "badge": "bg-yellow-100 text-yellow-800"},
            "critico":       {"color": "red",    "label": "Crítico",       "badge": "bg-red-100 text-red-800"},
            "expirado":      {"color": "gray",   "label": "Expirado",      "badge": "bg-gray-100 text-gray-700"},
            "archivado":     {"color": "gray",   "label": "Archivado",     "badge": "bg-gray-200 text-gray-600"},
        }

        tiene_permiso_moderar = usuario.tiene_permiso(Permiso.MODERAR_CONTENIDO)

        from app.repositories import NotificacionRepository
        no_leidas = NotificacionRepository(session).contar_no_leidas(usuario.id)

        return templates.TemplateResponse("index.html", {
            "request": request,
            "usuario": usuario_schema,
            # mode="json": serializa datetime/tipos anidados a primitivos JSON,
            # necesario porque la plantilla hace `{{ reportes | tojson }}`.
            "reportes": [r.model_dump(mode="json") for r in reportes],
            "estado_config": estado_config,
            "tipos_reporte": list(TIPOS_REPORTE_VALIDOS),
            "tiene_permiso_moderar": tiene_permiso_moderar,
            "no_leidas": no_leidas,
            "token": request.cookies.get("campusradar_token", ""),
        })
    finally:
        session.close()


# notificaciones

@router_web.get("/notificaciones", response_class=HTMLResponse)
def web_notificaciones(request: Request):
    """Página con las notificaciones del usuario y gestión de suscripciones."""
    session = SessionFactory()
    try:
        usuario = _get_usuario_desde_cookie(request, session)
        if usuario is None:
            return RedirectResponse(url="/web/login", status_code=302)

        from app.repositories import NotificacionRepository, SuscripcionRepository
        n_repo = NotificacionRepository(session)
        s_repo = SuscripcionRepository(session)

        return templates.TemplateResponse("notificaciones.html", {
            "request": request,
            "usuario": _usuario_a_schema(usuario),
            "notificaciones": [
                {"id": n.id, "reporte_id": n.reporte_id, "mensaje": n.mensaje, "leida": n.leida}
                for n in n_repo.listar_por_usuario(usuario.id)
            ],
            "suscripciones": [
                {"id": s.id, "tipo": s.tipo.value, "valor": s.valor}
                for s in s_repo.listar_por_usuario(usuario.id)
            ],
            "no_leidas": n_repo.contar_no_leidas(usuario.id),
        })
    finally:
        session.close()


def _usuario_web_o_login(request: Request):
    """Devuelve (usuario, None) si hay sesión, o (None, RedirectResponse) si no."""
    session = SessionFactory()
    try:
        usuario = _get_usuario_desde_cookie(request, session)
    finally:
        session.close()
    if usuario is None:
        return None, RedirectResponse(url="/web/login", status_code=302)
    return usuario, None


@router_web.post("/suscripciones")
def web_crear_suscripcion(request: Request, tipo: str = Form(...), valor: str = Form(...)):
    usuario, redirect = _usuario_web_o_login(request)
    if redirect:
        return redirect
    if tipo in {t.value for t in TipoSuscripcion} and valor.strip():
        with UnitOfWork(SessionFactory) as uow:
            uow.suscripciones.crear(usuario.id, TipoSuscripcion(tipo), valor)
            uow.commit()
    return RedirectResponse(url="/web/notificaciones", status_code=302)


@router_web.post("/suscripciones/{suscripcion_id}/eliminar")
def web_eliminar_suscripcion(suscripcion_id: int, request: Request):
    usuario, redirect = _usuario_web_o_login(request)
    if redirect:
        return redirect
    with UnitOfWork(SessionFactory) as uow:
        uow.suscripciones.eliminar(suscripcion_id, usuario.id)
        uow.commit()
    return RedirectResponse(url="/web/notificaciones", status_code=302)


@router_web.post("/notificaciones/leer-todas")
def web_leer_todas(request: Request):
    usuario, redirect = _usuario_web_o_login(request)
    if redirect:
        return redirect
    with UnitOfWork(SessionFactory) as uow:
        uow.notificaciones.marcar_todas_leidas(usuario.id)
        uow.commit()
    return RedirectResponse(url="/web/notificaciones", status_code=302)


@router_web.post("/notificaciones/{notificacion_id}/leer")
def web_leer_notificacion(notificacion_id: int, request: Request):
    usuario, redirect = _usuario_web_o_login(request)
    if redirect:
        return redirect
    with UnitOfWork(SessionFactory) as uow:
        uow.notificaciones.marcar_leida(notificacion_id, usuario.id)
        uow.commit()
    return RedirectResponse(url="/web/notificaciones", status_code=302)


#montar router
app.include_router(router_web)


# mish
#  api.py registró GET "/" devolviendo un JSON informativo, y este módulo registró
# GET "/" para el dashboard HTML (index). FastAPI resuelve las rutas por orden de
# registro, por lo que la de api.py (registrada primero) ganaba y ensombrecía el
# dashboard: "/" devolvía JSON y `index()` (que siembra los usuarios demo) nunca
# se ejecutaba. Como esta es la capa web —que por diseño "sobreescribe /"—,
# eliminamos la ruta JSON para que "/" sirva el dashboard.
app.router.routes = [
    r for r in app.router.routes
    if not (
        getattr(r, "path", None) == "/"
        and getattr(getattr(r, "endpoint", None), "__name__", "") == "root"
    )
]