"""
CampusRadar - Paso 4: API REST con FastAPI
==========================================

Este módulo expone todos los endpoints HTTP del sistema.
Importa domain.py, persistence.py y repositories.py — no contiene
lógica de negocio propia, solo orquesta llamadas al dominio y
devuelve respuestas serializadas vía schemas Pydantic.

DECISIONES DE DISEÑO CLAVE
---------------------------

1. Schemas Pydantic separados del dominio y del ORM:
   Se definen aquí tres "familias" de schemas por entidad:
   - `XxxCreate`  -> datos que el cliente envía (input)
   - `XxxOut`     -> datos que el servidor devuelve (output)
   - `XxxUpdate`  -> campos opcionales para PATCH (cuando aplica)
   Esto evita exponer campos internos (password_hash, _estado_forzado)
   y desacopla el contrato HTTP del modelo de dominio.

2. Autenticación JWT (Bearer token):
   - POST /auth/registro  -> crea usuario, devuelve token
   - POST /auth/login     -> valida credenciales, devuelve token
   El token lleva el `sub` (email) y `rol` del usuario.
   Cada endpoint protegido usa `Depends(get_current_user)` para
   extraer y validar el token, devolviendo el objeto de dominio
   correspondiente.
   Se usa passlib[bcrypt] para hashear contraseñas y python-jose
   para firmar/verificar JWT.

3. Inyección de dependencias (Depends):
   `get_db()` es un generador que abre una sesión SQLAlchemy y la
   cierra al terminar la request (patrón estándar FastAPI).
   `get_current_user()` depende de `get_db()` y del token Bearer.
   Esto garantiza que cada request tenga su propia sesión y su
   propio usuario autenticado.

4. Manejo de errores con HTTPException:
   Las excepciones del dominio (PermissionError, ValueError) y del
   repositorio (EntidadNoEncontrada, EmailDuplicado) se capturan y
   se convierten en respuestas HTTP apropiadas (400, 403, 404, 409).

5. Estructura de routers:
   La API se divide en routers temáticos montados bajo prefijos:
   - /auth       -> registro y login
   - /usuarios   -> perfil y listado (admin)
   - /reportes   -> CRUD, feed, búsqueda, filtros
   - /reportes/{id}/interacciones -> confirmar, desmentir, comentar, denunciar
   - /tags       -> listado de tags disponibles

6. Paginación simple:
   Los endpoints de listado aceptan `skip` y `limit` como query params.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Annotated

from fastapi import FastAPI, Depends, HTTPException, status, Query
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy.orm import Session

# capas
from app.domain import (
    Estudiante, Moderador, Administrador,
    Reporte, ReporteInfraestructura, ReporteActividadExtraprogramatica,
    ReporteAlertaEmergencia, ReporteEventoUniversitario, ReporteInformacionLogistica,
    Ubicacion, EstadoReporte, Permiso, TipoSuscripcion,
)
from app.persistence import (
    crear_engine, crear_tablas, crear_session_factory, RolUsuario,
)
from app.repositories import (
    UnitOfWork, EntidadNoEncontrada, EmailDuplicado,
)
from app.config import settings
from app.services import NotificacionService

# configuracion

# Configuración centralizada (variables de entorno + .env) vía app/config.py.
# Se re-exponen como constantes de módulo porque api_web.py las importa desde aquí.
SECRET_KEY = settings.secret_key
ALGORITHM = settings.algorithm
ACCESS_TOKEN_EXPIRE_MINUTES = settings.access_token_expire_minutes
DATABASE_URL = settings.database_url

# crear_engine acepta una URL completa (PostgreSQL/SQLite) o una ruta de archivo
# SQLite simple; así DATABASE_URL puede apuntar a Postgres sin cambiar el código.
engine = crear_engine(DATABASE_URL, echo=False)
crear_tablas(engine)
SessionFactory = crear_session_factory(engine)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


#schemas

class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    rol: str
    usuario_id: int
    nombre: str


class TokenData(BaseModel):
    email: str
    rol: str


# usuariso

class UsuarioCreate(BaseModel):
    nombre: str = Field(..., min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(..., min_length=6)
    rol: str = Field(default=RolUsuario.ESTUDIANTE)

    @field_validator("rol")
    @classmethod
    def rol_valido(cls, v: str) -> str:
        validos = {RolUsuario.ESTUDIANTE, RolUsuario.MODERADOR, RolUsuario.ADMINISTRADOR}
        if v not in validos:
            raise ValueError(f"Rol inválido. Opciones: {validos}")
        return v


class UsuarioOut(BaseModel):
    id: int
    nombre: str
    email: str
    rol: str
    puntaje_reputacion: int
    nivel_reputacion: str
    activo: bool
    fecha_registro: datetime

    model_config = {"from_attributes": True}


# ubicacion

class UbicacionSchema(BaseModel):
    edificio: str = Field(..., min_length=1, max_length=120)
    piso: Optional[int] = None
    zona: Optional[str] = None
    latitud: Optional[float] = Field(default=None, ge=-90, le=90)
    longitud: Optional[float] = Field(default=None, ge=-180, le=180)


#reportes

TIPOS_REPORTE_VALIDOS = {
    "infraestructura",
    "actividad_extraprogramatica",
    "alerta_emergencia",
    "evento_universitario",
    "informacion_logistica",
}

_TIPO_A_CLASE_DOMINIO = {
    "infraestructura": ReporteInfraestructura,
    "actividad_extraprogramatica": ReporteActividadExtraprogramatica,
    "alerta_emergencia": ReporteAlertaEmergencia,
    "evento_universitario": ReporteEventoUniversitario,
    "informacion_logistica": ReporteInformacionLogistica,
}


class ReporteCreate(BaseModel):
    tipo: str
    descripcion: str = Field(..., min_length=5, max_length=1000)
    ubicacion: UbicacionSchema
    tags: set[str] = Field(default_factory=set)

    @field_validator("tipo")
    @classmethod
    def tipo_valido(cls, v: str) -> str:
        if v not in TIPOS_REPORTE_VALIDOS:
            raise ValueError(f"Tipo inválido. Opciones: {TIPOS_REPORTE_VALIDOS}")
        return v


class ReporteOut(BaseModel):
    id: int
    tipo: str
    descripcion: str
    ubicacion: UbicacionSchema
    tags: list[str]
    estado: str
    esta_activo: bool
    prioridad: int
    total_confirmaciones: int
    total_desmentidos: int
    total_denuncias: int
    timestamp: datetime
    autor_id: int
    autor_nombre: str
    ya_voto: bool = False   # NUEVO
    comentarios: list[InteraccionOut] = Field(default_factory=list)   # NUEVO
    verificado_por_moderacion: bool = False

class ReporteListOut(BaseModel):
    total: int
    reportes: list[ReporteOut]


# interactuar

class ComentarioCreate(BaseModel):
    texto: str = Field(..., min_length=1, max_length=500)


class DenunciaCreate(BaseModel):
    motivo: str = Field(..., min_length=5, max_length=300)


class InteraccionOut(BaseModel):
    id: int
    tipo: str
    autor_id: int
    autor_nombre: str
    timestamp: datetime
    texto: Optional[str] = None
    motivo: Optional[str] = None


# tag

class TagOut(BaseModel):
    id: int
    nombre: str


# suscripciones y notificacionnes

class SuscripcionCreate(BaseModel):
    tipo: str   # 'tag' | 'edificio'
    valor: str = Field(..., min_length=1, max_length=120)

    @field_validator("tipo")
    @classmethod
    def tipo_valido(cls, v: str) -> str:
        validos = {t.value for t in TipoSuscripcion}
        if v not in validos:
            raise ValueError(f"Tipo inválido. Opciones: {validos}")
        return v


class SuscripcionOut(BaseModel):
    id: int
    tipo: str
    valor: str


class NotificacionOut(BaseModel):
    id: int
    reporte_id: int
    mensaje: str
    leida: bool
    timestamp: datetime


#helper para autenticar
def hashear_password(password: str) -> str:
    return pwd_context.hash(password)


def verificar_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def crear_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode["exp"] = expire
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


#dependencias
def get_db():
    """Generador de sesión SQLAlchemy. Una sesión por request."""
    session = SessionFactory()
    try:
        yield session
    finally:
        session.close()


DbDep = Annotated[Session, Depends(get_db)]
TokenDep = Annotated[str, Depends(oauth2_scheme)]


def get_current_user(token: TokenDep, db: DbDep):
    """
    Extrae y valida el JWT. Devuelve el objeto de dominio Usuario.
    Lanza 401 si el token es inválido o el usuario no existe/está inactivo.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token inválido o expirado.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    from app.repositories import UsuarioRepository
    repo = UsuarioRepository(db)
    usuario = repo.obtener_por_email(email)
    if usuario is None or not usuario.activo:
        raise credentials_exception
    return usuario


CurrentUser = Annotated[object, Depends(get_current_user)]


def get_current_moderador(current_user: CurrentUser):
    if not current_user.tiene_permiso(Permiso.MODERAR_CONTENIDO):
        raise HTTPException(status_code=403, detail="Se requiere rol de moderador o superior.")
    return current_user


def get_current_admin(current_user: CurrentUser):
    if not current_user.tiene_permiso(Permiso.GESTIONAR_USUARIOS):
        raise HTTPException(status_code=403, detail="Se requiere rol de administrador.")
    return current_user


ModDep = Annotated[object, Depends(get_current_moderador)]
AdminDep = Annotated[object, Depends(get_current_admin)]


#dominio -> salida
def _reporte_a_schema(reporte: Reporte, usuario_id: Optional[int] = None) -> ReporteOut:
    tipo = type(reporte).__name__.replace("Reporte", "").lower()
    # Mapear nombre de clase a tipo string
    tipo_map = {
        "infraestructura": "infraestructura",
        "actividadextraprogramatica": "actividad_extraprogramatica",
        "alertaemergencia": "alerta_emergencia",
        "eventouniversitario": "evento_universitario",
        "informacionlogistica": "informacion_logistica",
    }
    tipo_str = tipo_map.get(tipo, tipo)

    return ReporteOut(
        id=reporte.id,
        tipo=tipo_str,
        descripcion=reporte.descripcion,
        ubicacion=UbicacionSchema(
            edificio=reporte.ubicacion.edificio,
            piso=reporte.ubicacion.piso,
            zona=reporte.ubicacion.zona,
            latitud=reporte.ubicacion.latitud,
            longitud=reporte.ubicacion.longitud,
        ),
        tags=sorted(reporte.tags),
        estado=reporte.estado.value,
        esta_activo=reporte.esta_activo,
        prioridad=reporte.calcular_prioridad(),
        total_confirmaciones=reporte.total_confirmaciones,
        total_desmentidos=reporte.total_desmentidos,
        total_denuncias=reporte.total_denuncias,
        timestamp=reporte.timestamp,
        autor_id=reporte.autor.id,
        autor_nombre=reporte.autor.nombre,
        verificado_por_moderacion=reporte.fue_verificado_por_moderacion,
        ya_voto=reporte.usuario_ya_voto(usuario_id) if usuario_id is not None else False,
                comentarios=[
            InteraccionOut(
                id=c.id,
                tipo="comentario",
                autor_id=c.autor.id,
                autor_nombre=c.autor.nombre,
                timestamp=c.timestamp,
                texto=c.texto,
            )
            for c in reporte.comentarios
        ],
    )
    

def _usuario_a_schema(usuario) -> UsuarioOut:
    rol_map = {
        Estudiante: RolUsuario.ESTUDIANTE,
        Moderador: RolUsuario.MODERADOR,
        Administrador: RolUsuario.ADMINISTRADOR,
    }
    return UsuarioOut(
        id=usuario.id,
        nombre=usuario.nombre,
        email=usuario.email,
        rol=rol_map.get(type(usuario), "estudiante"),
        puntaje_reputacion=usuario.reputacion.puntaje,
        nivel_reputacion=usuario.reputacion.nivel.value,
        activo=usuario.activo,
        fecha_registro=usuario._fecha_registro,
    )


#aplicacion fastapi
app = FastAPI(
    title="CampusRadar API",
    description="Plataforma colaborativa de información universitaria - FCFM UChile",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # En producción: restringir al dominio del frontend
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


#router (auth)
from fastapi import APIRouter

router_auth = APIRouter(prefix="/auth", tags=["Autenticación"])


@router_auth.post("/registro", response_model=TokenOut, status_code=201)
def registrar_usuario(datos: UsuarioCreate, db: DbDep):
    """
    Registra un nuevo usuario y devuelve un JWT listo para usar.
    El password se hashea con bcrypt antes de persistirse.
    """
    clase_map = {
        RolUsuario.ESTUDIANTE: Estudiante,
        RolUsuario.MODERADOR: Moderador,
        RolUsuario.ADMINISTRADOR: Administrador,
    }
    clase = clase_map[datos.rol]
    password_hash = hashear_password(datos.password)
    usuario = clase(nombre=datos.nombre, email=datos.email, password_hash=password_hash)

    try:
        with UnitOfWork(SessionFactory) as uow:
            uow.usuarios.guardar(usuario)
            uow.commit()
    except EmailDuplicado:
        raise HTTPException(status_code=409, detail="El email ya está registrado.")

    token = crear_access_token({"sub": usuario.email, "rol": datos.rol})
    return TokenOut(
        access_token=token,
        rol=datos.rol,
        usuario_id=usuario.id,
        nombre=usuario.nombre,
    )


@router_auth.post("/login", response_model=TokenOut)
def login(form_data: Annotated[OAuth2PasswordRequestForm, Depends()], db: DbDep):
    """
    Autenticación estándar OAuth2 (username=email, password).
    Devuelve JWT si las credenciales son correctas.
    """
    from app.repositories import UsuarioRepository
    repo = UsuarioRepository(db)
    usuario = repo.obtener_por_email(form_data.username)

    if usuario is None or not verificar_password(form_data.password, usuario._password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email o contraseña incorrectos.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not usuario.activo:
        raise HTTPException(status_code=403, detail="Cuenta desactivada.")

    rol_map = {Estudiante: RolUsuario.ESTUDIANTE, Moderador: RolUsuario.MODERADOR,
               Administrador: RolUsuario.ADMINISTRADOR}
    rol = rol_map.get(type(usuario), RolUsuario.ESTUDIANTE)

    token = crear_access_token({"sub": usuario.email, "rol": rol})
    return TokenOut(
        access_token=token,
        rol=rol,
        usuario_id=usuario.id,
        nombre=usuario.nombre,
    )


@router_auth.get("/me", response_model=UsuarioOut)
def perfil_propio(current_user: CurrentUser):
    """Devuelve el perfil del usuario autenticado."""
    return _usuario_a_schema(current_user)


#touter (usuarios)
router_usuarios = APIRouter(prefix="/usuarios", tags=["Usuarios"])


@router_usuarios.get("/", response_model=list[UsuarioOut])
def listar_usuarios(
    current_user: AdminDep,
    db: DbDep,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    """Lista todos los usuarios. Solo administradores."""
    from app.repositories import UsuarioRepository
    repo = UsuarioRepository(db)
    todos = repo.listar_todos()
    return [_usuario_a_schema(u) for u in todos[skip: skip + limit]]


@router_usuarios.get("/{usuario_id}", response_model=UsuarioOut)
def obtener_usuario(usuario_id: int, db: DbDep, current_user: CurrentUser):
    """Obtiene el perfil público de un usuario por ID."""
    from app.repositories import UsuarioRepository
    repo = UsuarioRepository(db)
    usuario = repo.obtener_por_id(usuario_id)
    if usuario is None:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")
    return _usuario_a_schema(usuario)


@router_usuarios.delete("/{usuario_id}/desactivar", status_code=204)
def desactivar_usuario(usuario_id: int, db: DbDep, current_user: AdminDep):
    """Desactiva (banea) una cuenta. Solo administradores."""
    from app.repositories import UsuarioRepository
    repo = UsuarioRepository(db)
    usuario = repo.obtener_por_id(usuario_id)
    if usuario is None:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")
    usuario.desactivar()
    with UnitOfWork(SessionFactory) as uow:
        uow.usuarios.actualizar(usuario)
        uow.commit()


#router (reportes)
router_reportes = APIRouter(prefix="/reportes", tags=["Reportes"])


@router_reportes.post("/", response_model=ReporteOut, status_code=201)
def crear_reporte(datos: ReporteCreate, db: DbDep, current_user: CurrentUser):
    """
    Crea un nuevo reporte. El autor es el usuario autenticado.
    """
    clase = _TIPO_A_CLASE_DOMINIO[datos.tipo]
    ubicacion = Ubicacion(
        edificio=datos.ubicacion.edificio,
        piso=datos.ubicacion.piso,
        zona=datos.ubicacion.zona,
        latitud=datos.ubicacion.latitud,
        longitud=datos.ubicacion.longitud,
    )

    try:
        reporte = clase(
            autor=current_user,
            descripcion=datos.descripcion,
            ubicacion=ubicacion,
            tags=datos.tags,
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))

    with UnitOfWork(SessionFactory) as uow:
        uow.reportes.guardar(reporte)
        uow.commit()

    # Notificar (best-effort) a quienes siguen un tag o el edificio del reporte.
    # Un fallo aquí no debe impedir la publicación del reporte ya guardado.
    try:
        NotificacionService(SessionFactory).notificar_nuevo_reporte(reporte)
    except Exception:
        pass

    return _reporte_a_schema(reporte, usuario_id=current_user.id)


@router_reportes.get("/feed", response_model=ReporteListOut)
def feed_principal(
    db: DbDep,
    current_user: CurrentUser,
    limite: int = Query(20, ge=1, le=100),
):
    """
    Feed principal: reportes activos ordenados por prioridad descendente.
    """
    from app.repositories import ReporteRepository
    repo = ReporteRepository(db)
    reportes = repo.feed_ordenado(limite=limite)
    return ReporteListOut(
        total=len(reportes),
        reportes=[_reporte_a_schema(r, usuario_id=current_user.id) for r in reportes],
    )


@router_reportes.get("/buscar", response_model=ReporteListOut)
def buscar_reportes(
    db: DbDep,
    current_user: CurrentUser,
    tags: Optional[str] = Query(None, description="Tags separados por coma. Ej: microondas,emergencia"),
    edificio: Optional[str] = Query(None),
    tipo: Optional[str] = Query(None),
    autor_id: Optional[int] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    """
    Búsqueda flexible con filtros combinables:
    - `tags`: uno o más tags separados por coma
    - `edificio`: nombre exacto del edificio (case-insensitive)
    - `tipo`: tipo de reporte
    - `autor_id`: ID del autor
    """
    from app.repositories import ReporteRepository
    repo = ReporteRepository(db)

    if tags:
        set_tags = {t.strip() for t in tags.split(",")}
        reportes = repo.buscar_por_tags(set_tags)
    elif edificio:
        reportes = repo.buscar_por_edificio(edificio)
    elif tipo:
        if tipo not in TIPOS_REPORTE_VALIDOS:
            raise HTTPException(status_code=400, detail=f"Tipo inválido: {tipo}")
        reportes = repo.buscar_por_tipo(tipo)
    elif autor_id:
        reportes = repo.buscar_por_autor(autor_id)
    else:
        reportes = repo.listar_activos()

    pagina = reportes[skip: skip + limit]
    return ReporteListOut(
        total=len(reportes),
        reportes=[_reporte_a_schema(r, usuario_id=current_user.id) for r in pagina],
    )


@router_reportes.get("/mapa", response_model=list[ReporteOut])
def reportes_para_mapa(
    db: DbDep,
    current_user: CurrentUser,
    lat_min: Optional[float] = Query(None),
    lat_max: Optional[float] = Query(None),
    lon_min: Optional[float] = Query(None),
    lon_max: Optional[float] = Query(None),
):
    """
    Devuelve reportes geolocalizados. Si se pasan los 4 parámetros de
    bounding box, filtra por zona geográfica. Si no, devuelve todos
    los que tengan coordenadas.
    """
    from app.repositories import ReporteRepository
    repo = ReporteRepository(db)

    if all(p is not None for p in [lat_min, lat_max, lon_min, lon_max]):
        reportes = repo.buscar_por_zona_geografica(lat_min, lat_max, lon_min, lon_max)
    else:
        todos = repo.listar_activos()
        reportes = [r for r in todos if r.ubicacion.latitud is not None]

    return [_reporte_a_schema(r, usuario_id=current_user.id) for r in reportes]


@router_reportes.get("/{reporte_id}", response_model=ReporteOut)
def obtener_reporte(reporte_id: int, db: DbDep, current_user: CurrentUser):
    """Obtiene un reporte por ID con su estado calculado al momento."""
    from app.repositories import ReporteRepository
    repo = ReporteRepository(db)
    reporte = repo.obtener_por_id(reporte_id)
    if reporte is None:
        raise HTTPException(status_code=404, detail="Reporte no encontrado.")
    return _reporte_a_schema(reporte, usuario_id=current_user.id)


@router_reportes.delete("/{reporte_id}/archivar", status_code=204)
def archivar_reporte(reporte_id: int, db: DbDep, current_user: ModDep):
    """Archiva un reporte manualmente. Solo moderadores y admins."""
    from app.repositories import ReporteRepository
    repo = ReporteRepository(db)
    reporte = repo.obtener_por_id(reporte_id)
    if reporte is None:
        raise HTTPException(status_code=404, detail="Reporte no encontrado.")
    try:
        reporte.archivar(current_user)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))

    with UnitOfWork(SessionFactory) as uow:
        uow.reportes.actualizar_estado_forzado(reporte)
        uow.commit()

@router_reportes.patch("/{reporte_id}/verificar", response_model=ReporteOut)
def verificar_reporte(reporte_id: int, db: DbDep, current_user: ModDep):
    """Verifica un reporte manualmente. Solo moderadores y admins."""
    from app.repositories import ReporteRepository
    repo = ReporteRepository(db)
    reporte = repo.obtener_por_id(reporte_id)
    if reporte is None:
        raise HTTPException(status_code=404, detail="Reporte no encontrado.")
    try:
        reporte.verificar(current_user)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))

    with UnitOfWork(SessionFactory) as uow:
        uow.reportes.actualizar_estado_forzado(reporte)
        uow.commit()

    return _reporte_a_schema(reporte, usuario_id=current_user.id)

#router (reportes-id-interacciones)
router_interacciones = APIRouter(
    prefix="/reportes/{reporte_id}",
    tags=["Interacciones"],
)


def _cargar_reporte_o_404(reporte_id: int, db: Session) -> Reporte:
    from app.repositories import ReporteRepository
    repo = ReporteRepository(db)
    reporte = repo.obtener_por_id(reporte_id)
    if reporte is None:
        raise HTTPException(status_code=404, detail="Reporte no encontrado.")
    return reporte


@router_interacciones.post("/confirmar", response_model=ReporteOut)
def confirmar_reporte(reporte_id: int, db: DbDep, current_user: CurrentUser):
    """Voto positivo sobre un reporte. Un usuario solo puede votar una vez."""
    reporte = _cargar_reporte_o_404(reporte_id, db)
    try:
        confirmacion = reporte.confirmar(current_user)
    except (PermissionError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    with UnitOfWork(SessionFactory) as uow:
        uow.interacciones.guardar(confirmacion, reporte_id=reporte_id)
        uow.usuarios.actualizar(reporte.autor)   # sincronizar reputación
        uow.commit()

    return _reporte_a_schema(reporte, usuario_id=current_user.id)


@router_interacciones.post("/desmentir", response_model=ReporteOut)
def desmentir_reporte(reporte_id: int, db: DbDep, current_user: CurrentUser):
    """Voto negativo sobre un reporte."""
    reporte = _cargar_reporte_o_404(reporte_id, db)
    try:
        desmentido = reporte.desmentir(current_user)
    except (PermissionError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    with UnitOfWork(SessionFactory) as uow:
        uow.interacciones.guardar(desmentido, reporte_id=reporte_id)
        uow.usuarios.actualizar(reporte.autor)
        uow.commit()

    return _reporte_a_schema(reporte, usuario_id=current_user.id)


@router_interacciones.post("/comentar", response_model=InteraccionOut, status_code=201)
def comentar_reporte(
    reporte_id: int,
    datos: ComentarioCreate,
    db: DbDep,
    current_user: CurrentUser,
):
    """Agrega un comentario textual al reporte."""
    reporte = _cargar_reporte_o_404(reporte_id, db)
    try:
        comentario = reporte.comentar(current_user, datos.texto)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))

    with UnitOfWork(SessionFactory) as uow:
        uow.interacciones.guardar(comentario, reporte_id=reporte_id)
        uow.commit()

    return InteraccionOut(
        id=comentario.id,
        tipo="comentario",
        autor_id=current_user.id,
        autor_nombre=current_user.nombre,
        timestamp=comentario.timestamp,
        texto=comentario.texto,
    )


@router_interacciones.post("/denunciar", response_model=InteraccionOut, status_code=201)
def denunciar_reporte(
    reporte_id: int,
    datos: DenunciaCreate,
    db: DbDep,
    current_user: CurrentUser,
):
    """Denuncia un reporte como inapropiado o erróneo."""
    reporte = _cargar_reporte_o_404(reporte_id, db)
    try:
        denuncia = reporte.denunciar(current_user, datos.motivo)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))

    with UnitOfWork(SessionFactory) as uow:
        uow.interacciones.guardar(denuncia, reporte_id=reporte_id)
        uow.usuarios.actualizar(reporte.autor)
        uow.commit()

    return InteraccionOut(
        id=denuncia.id,
        tipo="denuncia",
        autor_id=current_user.id,
        autor_nombre=current_user.nombre,
        timestamp=denuncia.timestamp,
        motivo=denuncia.motivo,
    )


@router_interacciones.get("/interacciones", response_model=list[InteraccionOut])
def listar_interacciones(reporte_id: int, db: DbDep, current_user: CurrentUser):
    """Lista todas las interacciones de un reporte en orden cronológico."""
    from app.repositories import InteraccionRepository, UsuarioRepository
    i_repo = InteraccionRepository(db)
    u_repo = UsuarioRepository(db)
    orms = i_repo.listar_por_reporte(reporte_id)
    result = []
    for i_orm in orms:
        autor = u_repo.obtener_por_id(i_orm.autor_id)
        result.append(InteraccionOut(
            id=i_orm.id,
            tipo=i_orm.tipo_interaccion,
            autor_id=i_orm.autor_id,
            autor_nombre=autor.nombre if autor else "Desconocido",
            timestamp=i_orm.timestamp,
            texto=i_orm.texto,
            motivo=i_orm.motivo,
        ))
    return result


#router tag
router_tags = APIRouter(prefix="/tags", tags=["Tags"])


@router_tags.get("/", response_model=list[TagOut])
def listar_tags(db: DbDep, current_user: CurrentUser):
    """Devuelve todos los tags registrados en el sistema."""
    from app.repositories import TagRepository
    repo = TagRepository(db)
    return [TagOut(id=t.id, nombre=t.nombre) for t in repo.listar_todos()]


#router suscripciones
router_suscripciones = APIRouter(prefix="/suscripciones", tags=["Suscripciones"])


@router_suscripciones.post("/", response_model=SuscripcionOut, status_code=201)
def crear_suscripcion(datos: SuscripcionCreate, db: DbDep, current_user: CurrentUser):
    """Sigue un tag o un edificio. Idempotente (no crea duplicados)."""
    with UnitOfWork(SessionFactory) as uow:
        s = uow.suscripciones.crear(current_user.id, TipoSuscripcion(datos.tipo), datos.valor)
        uow.commit()
    return SuscripcionOut(id=s.id, tipo=s.tipo.value, valor=s.valor)


@router_suscripciones.get("/", response_model=list[SuscripcionOut])
def listar_suscripciones(db: DbDep, current_user: CurrentUser):
    """Lista las suscripciones del usuario autenticado."""
    from app.repositories import SuscripcionRepository
    repo = SuscripcionRepository(db)
    return [
        SuscripcionOut(id=s.id, tipo=s.tipo.value, valor=s.valor)
        for s in repo.listar_por_usuario(current_user.id)
    ]


@router_suscripciones.delete("/{suscripcion_id}", status_code=204)
def eliminar_suscripcion(suscripcion_id: int, db: DbDep, current_user: CurrentUser):
    """Deja de seguir (elimina una suscripción propia)."""
    with UnitOfWork(SessionFactory) as uow:
        ok = uow.suscripciones.eliminar(suscripcion_id, current_user.id)
        uow.commit()
    if not ok:
        raise HTTPException(status_code=404, detail="Suscripción no encontrada.")


#router notificaciones
router_notificaciones = APIRouter(prefix="/notificaciones", tags=["Notificaciones"])


@router_notificaciones.get("/", response_model=list[NotificacionOut])
def listar_notificaciones(
    db: DbDep,
    current_user: CurrentUser,
    solo_no_leidas: bool = Query(False),
):
    """Lista las notificaciones del usuario (más recientes primero)."""
    from app.repositories import NotificacionRepository
    repo = NotificacionRepository(db)
    notifs = repo.listar_por_usuario(current_user.id, solo_no_leidas=solo_no_leidas)
    return [
        NotificacionOut(
            id=n.id, reporte_id=n.reporte_id, mensaje=n.mensaje,
            leida=n.leida, timestamp=n.timestamp,
        )
        for n in notifs
    ]


@router_notificaciones.get("/conteo")
def conteo_no_leidas(db: DbDep, current_user: CurrentUser):
    """Devuelve cuántas notificaciones sin leer tiene el usuario."""
    from app.repositories import NotificacionRepository
    repo = NotificacionRepository(db)
    return {"no_leidas": repo.contar_no_leidas(current_user.id)}


@router_notificaciones.post("/{notificacion_id}/leer", status_code=204)
def leer_notificacion(notificacion_id: int, db: DbDep, current_user: CurrentUser):
    """Marca una notificación como leída."""
    with UnitOfWork(SessionFactory) as uow:
        ok = uow.notificaciones.marcar_leida(notificacion_id, current_user.id)
        uow.commit()
    if not ok:
        raise HTTPException(status_code=404, detail="Notificación no encontrada.")


@router_notificaciones.post("/leer-todas")
def leer_todas_notificaciones(db: DbDep, current_user: CurrentUser):
    """Marca todas las notificaciones del usuario como leídas."""
    with UnitOfWork(SessionFactory) as uow:
        n = uow.notificaciones.marcar_todas_leidas(current_user.id)
        uow.commit()
    return {"marcadas": n}


#montar notificaciones y routers
app.include_router(router_auth)
app.include_router(router_usuarios)
app.include_router(router_reportes)
app.include_router(router_interacciones)
app.include_router(router_tags)
app.include_router(router_suscripciones)
app.include_router(router_notificaciones)


@app.get("/", tags=["Root"])
def root():
    return {
        "proyecto": "CampusRadar",
        "version": "1.0.0",
        "docs": "/docs",
        "universidad": "Universidad de Chile - FCFM",
    }


#entry pint
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.api:app", host="0.0.0.0", port=8000, reload=True)
