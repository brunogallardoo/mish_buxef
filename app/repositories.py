"""
CampusRadar - Paso 3: Capa de Repositorios
===========================================

Este módulo implementa el patrón Repository: cada clase actúa como la
única puerta de entrada a la base de datos para una entidad del dominio.
El resto del sistema (API, lógica de negocio) solo habla con repositorios,
nunca con SQLAlchemy directamente.

DECISIONES DE DISEÑO CLAVE
---------------------------

1. Patrón Repository (separación de responsabilidades):
   La API y el dominio no conocen SQLAlchemy. Los repositorios son la
   única capa que importa modelos ORM y sesiones. Si mañana se cambia
   de SQLite a PostgreSQL, o de SQLAlchemy a otro ORM, solo cambia
   esta capa.

2. Mappers bidireccionales (ORM <-> Dominio):
   Cada repositorio tiene funciones privadas `_orm_a_dominio()` y
   `_dominio_a_orm()` que traducen entre los dos mundos. El dominio
   vive en memoria (objetos Python puros), el ORM vive en la BD.
   Se reconstruye el objeto de dominio completo con todas sus
   interacciones para que la lazy evaluation del estado funcione
   correctamente.

3. Unit of Work implícito vía Session:
   La `Session` de SQLAlchemy actúa como Unit of Work: agrupa
   operaciones en una transacción. Los repositorios reciben la sesión
   como dependencia (inyección), lo que facilita el testing (se puede
   pasar una sesión de test con rollback).

4. Discriminadores para STI:
   Al reconstruir un objeto de dominio desde la BD, se usa el campo
   `tipo_reporte` / `rol` para instanciar la subclase correcta
   (ej: `ReporteAlertaEmergenciaORM` -> `ReporteAlertaEmergencia`).
   Esto respeta el polimorfismo definido en domain.py.

5. Reconstrucción de estado forzado:
   Si `reporte_orm.estado_forzado` no es None, se setea
   `reporte._estado_forzado` para preservar archivados/forzados
   que un moderador haya aplicado manualmente.

6. Passwords:
   Los repositorios nunca almacenan ni devuelven passwords en texto
   plano. Solo manejan `password_hash`. El hashing real (bcrypt, etc.)
   es responsabilidad de la capa de autenticación (Paso 4).

7. Métodos de búsqueda y filtrado:
   Se incluyen queries para las funcionalidades clave del enunciado:
   buscar por tags, filtrar por ubicación/edificio, obtener feed
   ordenado por prioridad calculada, y búsqueda de reportes activos.
   Las queries usan SQLAlchemy 2.0 (`select()`) en vez del estilo
   legado (`.query()`).
"""

from __future__ import annotations

from typing import Optional
from datetime import datetime, timezone

from sqlalchemy import select, func
from sqlalchemy.orm import Session


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """
    Normaliza un datetime cargado desde la BD a *timezone-aware*.

    SQLite devuelve datetimes naive (sin tz) y PostgreSQL los devuelve aware.
    Para que el dominio (que trabaja en UTC aware, ver `domain._ahora`) no mezcle
    naive con aware, a los naive se les asume UTC; los aware se dejan igual.
    """
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=timezone.utc)

# dominio
from app.domain import (
    Usuario, Estudiante, Moderador, Administrador,
    Reporte, ReporteInfraestructura, ReporteActividadExtraprogramatica,
    ReporteAlertaEmergencia, ReporteEventoUniversitario, ReporteInformacionLogistica,
    Reputacion, Ubicacion,
    Interaccion, Comentario, Confirmacion, Desmentido, Denuncia,
    EstadoReporte, TipoInteraccion,
    Permiso,
    Suscripcion, Notificacion, TipoSuscripcion,
)

# orm
from app.persistence import (
    UsuarioORM, EstudianteORM, ModeradorORM, AdministradorORM, RolUsuario,
    ReporteORM, ReporteInfraestructuraORM, ReporteActividadExtraprogramaticaORM,
    ReporteAlertaEmergenciaORM, ReporteEventoUniversitarioORM,
    ReporteInformacionLogisticaORM, TipoReporte,
    InteraccionORM, ComentarioORM, ConfirmacionORM, DesmentidoORM, DenunciaORM,
    TagORM, reporte_tags,
    SuscripcionORM, NotificacionORM,
)


# excepciones
class EntidadNoEncontrada(Exception):
    """Se lanza cuando se busca un ID que no existe en la BD."""


class EmailDuplicado(Exception):
    """Se lanza al intentar registrar un email ya existente."""


# mappers
# usuarios

def _rol_a_clase_dominio(rol: str) -> type[Usuario]:
    """Devuelve la clase de dominio correcta según el discriminador de rol."""
    return {
        RolUsuario.ESTUDIANTE: Estudiante,
        RolUsuario.MODERADOR: Moderador,
        RolUsuario.ADMINISTRADOR: Administrador,
    }[rol]


def _rol_a_clase_orm(rol: str) -> type[UsuarioORM]:
    """Devuelve la clase ORM correcta según el discriminador de rol."""
    return {
        RolUsuario.ESTUDIANTE: EstudianteORM,
        RolUsuario.MODERADOR: ModeradorORM,
        RolUsuario.ADMINISTRADOR: AdministradorORM,
    }[rol]


def _usuario_orm_a_dominio(u_orm: UsuarioORM) -> Usuario:
    """
    Reconstruye un objeto de dominio Usuario desde su contraparte ORM.
    Reinyecta el ID (que viene de la BD) y reconstruye la Reputacion
    a partir del puntaje persistido.
    """
    clase = _rol_a_clase_dominio(u_orm.rol)
    # Instanciamos sin pasar por __init__ para evitar que el contador
    # de IDs incremente de nuevo. Usamos object.__new__ y seteamos
    # los atributos privados manualmente.
    usuario = object.__new__(clase)
    usuario._id = u_orm.id
    usuario._nombre = u_orm.nombre
    usuario._email = u_orm.email
    usuario._password_hash = u_orm.password_hash
    usuario._reputacion = Reputacion(puntaje=u_orm.puntaje_reputacion)
    usuario._activo = u_orm.activo
    usuario._fecha_registro = _as_utc(u_orm.fecha_registro)
    return usuario


def _usuario_dominio_a_orm(usuario: Usuario) -> UsuarioORM:
    """
    Crea un nuevo objeto ORM desde un objeto de dominio.
    Se usa al persistir por primera vez (INSERT); para UPDATEs se
    actualiza el ORM existente directamente.
    """
    rol = {
        Estudiante: RolUsuario.ESTUDIANTE,
        Moderador: RolUsuario.MODERADOR,
        Administrador: RolUsuario.ADMINISTRADOR,
    }[type(usuario)]

    clase_orm = _rol_a_clase_orm(rol)
    return clase_orm(
        nombre=usuario.nombre,
        email=usuario.email,
        password_hash=usuario._password_hash,
        rol=rol,
        puntaje_reputacion=usuario.reputacion.puntaje,
        activo=usuario.activo,
    )


# interacciones

def _interaccion_orm_a_dominio(
    i_orm: InteraccionORM,
    autor_dominio: Usuario,
) -> Interaccion:
    """Reconstruye una Interaccion de dominio desde su ORM."""
    tipo = i_orm.tipo_interaccion

    if tipo == TipoInteraccion.COMENTARIO.value:
        obj = object.__new__(Comentario)
        obj._texto = i_orm.texto or ""
    elif tipo == TipoInteraccion.CONFIRMACION.value:
        obj = object.__new__(Confirmacion)
    elif tipo == TipoInteraccion.DESMENTIDO.value:
        obj = object.__new__(Desmentido)
    elif tipo == TipoInteraccion.DENUNCIA.value:
        obj = object.__new__(Denuncia)
        obj._motivo = i_orm.motivo or ""
    else:
        raise ValueError(f"Tipo de interacción desconocido: {tipo}")

    obj._id = i_orm.id
    obj._autor = autor_dominio
    obj._timestamp = _as_utc(i_orm.timestamp)
    return obj


def _interaccion_dominio_a_orm(
    interaccion: Interaccion,
    reporte_id: int,
) -> InteraccionORM:
    """Crea el ORM de una interacción lista para persistir."""
    tipo = interaccion.tipo.value
    kwargs = dict(
        tipo_interaccion=tipo,
        reporte_id=reporte_id,
        autor_id=interaccion.autor.id,
        timestamp=interaccion.timestamp,
    )

    if isinstance(interaccion, Comentario):
        return ComentarioORM(**kwargs, texto=interaccion.texto)
    if isinstance(interaccion, Confirmacion):
        return ConfirmacionORM(**kwargs)
    if isinstance(interaccion, Desmentido):
        return DesmentidoORM(**kwargs)
    if isinstance(interaccion, Denuncia):
        return DenunciaORM(**kwargs, motivo=interaccion.motivo)

    raise TypeError(f"Interacción no soportada: {type(interaccion)}")


# reportes

_TIPO_REPORTE_A_CLASE_DOMINIO: dict[str, type[Reporte]] = {
    TipoReporte.INFRAESTRUCTURA: ReporteInfraestructura,
    TipoReporte.ACTIVIDAD_EXTRAPROGRAMATICA: ReporteActividadExtraprogramatica,
    TipoReporte.ALERTA_EMERGENCIA: ReporteAlertaEmergencia,
    TipoReporte.EVENTO_UNIVERSITARIO: ReporteEventoUniversitario,
    TipoReporte.INFORMACION_LOGISTICA: ReporteInformacionLogistica,
}

_TIPO_REPORTE_A_CLASE_ORM: dict[str, type[ReporteORM]] = {
    TipoReporte.INFRAESTRUCTURA: ReporteInfraestructuraORM,
    TipoReporte.ACTIVIDAD_EXTRAPROGRAMATICA: ReporteActividadExtraprogramaticaORM,
    TipoReporte.ALERTA_EMERGENCIA: ReporteAlertaEmergenciaORM,
    TipoReporte.EVENTO_UNIVERSITARIO: ReporteEventoUniversitarioORM,
    TipoReporte.INFORMACION_LOGISTICA: ReporteInformacionLogisticaORM,
}

_CLASE_DOMINIO_A_TIPO_REPORTE: dict[type[Reporte], str] = {
    v: k for k, v in _TIPO_REPORTE_A_CLASE_DOMINIO.items()
}


def _reporte_orm_a_dominio(
    r_orm: ReporteORM,
    usuarios_cache: dict[int, Usuario],
) -> Reporte:
    """
    Reconstruye un objeto Reporte de dominio desde su ORM, incluyendo
    todas sus interacciones. El `usuarios_cache` evita rehidratar el
    mismo usuario múltiples veces dentro de la misma operación.

    Este es el método más crítico del Paso 3: garantiza que la lazy
    evaluation del `estado` (definida en domain.py) funcione
    correctamente al tener todos los datos en memoria.
    """
    clase = _TIPO_REPORTE_A_CLASE_DOMINIO.get(r_orm.tipo_reporte)
    if clase is None:
        raise ValueError(f"Tipo de reporte desconocido: {r_orm.tipo_reporte!r}")

    autor = usuarios_cache[r_orm.autor_id]
    ubicacion = Ubicacion(
        edificio=r_orm.ubicacion_edificio,
        piso=r_orm.ubicacion_piso,
        zona=r_orm.ubicacion_zona,
        latitud=r_orm.ubicacion_lat,
        longitud=r_orm.ubicacion_lon,
    )

    # Instanciar sin __init__ para no disparar validaciones de permiso
    # ni incrementar el contador de IDs del dominio.
    reporte = object.__new__(clase)
    reporte._id = r_orm.id
    reporte._autor = autor
    reporte._descripcion = r_orm.descripcion
    reporte._ubicacion = ubicacion
    reporte._tags = set(t.nombre for t in r_orm.tags)
    reporte._timestamp = _as_utc(r_orm.timestamp)
    reporte._comentarios = []
    reporte._confirmaciones = []
    reporte._desmentidos = []
    reporte._denuncias = []
    reporte._usuarios_que_votaron = set()
    reporte._estado_forzado = r_orm.estado_forzado  # None o valor manual

    # Reconstruir interacciones en orden cronológico
    for i_orm in sorted(r_orm.interacciones, key=lambda i: i.timestamp):
        # El autor de la interacción puede ser distinto del autor del reporte
        if i_orm.autor_id not in usuarios_cache:
            # Usuario no cargado previamente: se salta (no debería ocurrir
            # con eager loading correcto, pero es una salvaguarda).
            continue
        autor_interaccion = usuarios_cache[i_orm.autor_id]
        interaccion = _interaccion_orm_a_dominio(i_orm, autor_interaccion)

        tipo = i_orm.tipo_interaccion
        if tipo == TipoInteraccion.COMENTARIO.value:
            reporte._comentarios.append(interaccion)
        elif tipo == TipoInteraccion.CONFIRMACION.value:
            reporte._confirmaciones.append(interaccion)
            reporte._usuarios_que_votaron.add(autor_interaccion.id)
        elif tipo == TipoInteraccion.DESMENTIDO.value:
            reporte._desmentidos.append(interaccion)
            reporte._usuarios_que_votaron.add(autor_interaccion.id)
        elif tipo == TipoInteraccion.DENUNCIA.value:
            reporte._denuncias.append(interaccion)

    return reporte


def _reporte_dominio_a_orm(
    reporte: Reporte,
    tipo_reporte: str,
    tags_orm: list[TagORM],
) -> ReporteORM:
    """
    Crea el ORM de un reporte nuevo. Los tags ya deben existir en la BD
    (o ser nuevos objetos TagORM pendientes de flush) y se pasan como
    lista para armar la relación many-to-many.
    """
    clase_orm = _TIPO_REPORTE_A_CLASE_ORM[tipo_reporte]
    return clase_orm(
        autor_id=reporte.autor.id,
        descripcion=reporte.descripcion,
        ubicacion_edificio=reporte.ubicacion.edificio,
        ubicacion_piso=reporte.ubicacion.piso,
        ubicacion_zona=reporte.ubicacion.zona,
        ubicacion_lat=reporte.ubicacion.latitud,
        ubicacion_lon=reporte.ubicacion.longitud,
        timestamp=reporte.timestamp,
        estado_forzado=reporte._estado_forzado,
        tags=tags_orm,
    )


# REPOSITORIO
class UsuarioRepository:
    """
    Gestiona la persistencia de usuarios.

    Métodos:
      - guardar(usuario)       -> INSERT de usuario nuevo
      - actualizar(usuario)    -> UPDATE de puntaje_reputacion y activo
      - obtener_por_id(id)     -> Usuario | None
      - obtener_por_email(email) -> Usuario | None
      - listar_todos()         -> list[Usuario]
      - existe_email(email)    -> bool
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def guardar(self, usuario: Usuario) -> Usuario:
        """
        Persiste un usuario nuevo. Lanza `EmailDuplicado` si el email
        ya existe. Devuelve el mismo usuario con el `_id` actualizado
        desde la BD (autoincrement).
        """
        if self.existe_email(usuario.email):
            raise EmailDuplicado(f"El email {usuario.email!r} ya está registrado.")

        u_orm = _usuario_dominio_a_orm(usuario)
        self._session.add(u_orm)
        self._session.flush()          # genera el ID sin commitear
        usuario._id = u_orm.id         # sincroniza el ID al objeto dominio
        return usuario

    def actualizar(self, usuario: Usuario) -> None:
        """
        Sincroniza los campos mutables del dominio (reputación, activo)
        de vuelta a la BD. Se llama después de confirmar/desmentir
        para persistir los cambios de puntaje.
        """
        stmt = select(UsuarioORM).where(UsuarioORM.id == usuario.id)
        u_orm = self._session.scalars(stmt).one_or_none()
        if u_orm is None:
            raise EntidadNoEncontrada(f"Usuario id={usuario.id} no encontrado.")
        u_orm.puntaje_reputacion = usuario.reputacion.puntaje
        u_orm.activo = usuario.activo

    def obtener_por_id(self, usuario_id: int) -> Optional[Usuario]:
        """Devuelve el Usuario de dominio o None si no existe."""
        u_orm = self._session.get(UsuarioORM, usuario_id)
        if u_orm is None:
            return None
        return _usuario_orm_a_dominio(u_orm)

    def obtener_por_email(self, email: str) -> Optional[Usuario]:
        """Busca por email (case-sensitive). Usado en autenticación."""
        stmt = select(UsuarioORM).where(UsuarioORM.email == email)
        u_orm = self._session.scalars(stmt).one_or_none()
        return _usuario_orm_a_dominio(u_orm) if u_orm else None

    def listar_todos(self) -> list[Usuario]:
        stmt = select(UsuarioORM)
        return [_usuario_orm_a_dominio(u) for u in self._session.scalars(stmt).all()]

    def existe_email(self, email: str) -> bool:
        stmt = select(func.count()).select_from(UsuarioORM).where(UsuarioORM.email == email)
        return self._session.scalar(stmt) > 0


#tags
class TagRepository:
    """
    Gestiona la tabla `tags` y la tabla de asociación `reporte_tags`.

    El método principal es `obtener_o_crear()`: dado un set de nombres
    de tags (strings), devuelve los TagORM existentes y crea los nuevos,
    todo dentro de la misma sesión. Esto garantiza que los tags sean
    únicos en la BD (la columna `nombre` tiene `unique=True`).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def obtener_o_crear(self, nombres: set[str]) -> list[TagORM]:
        """
        Dado un conjunto de nombres, devuelve los TagORM correspondientes.
        Si alguno no existe, lo crea. Hace flush para que los nuevos
        tags tengan ID antes de usarse en relaciones.
        """
        if not nombres:
            return []

        nombres_lower = {n.lower().strip() for n in nombres}

        # Traer los que ya existen
        stmt = select(TagORM).where(TagORM.nombre.in_(nombres_lower))
        existentes = {t.nombre: t for t in self._session.scalars(stmt).all()}

        # Crear los que faltan
        nuevos = []
        for nombre in nombres_lower:
            if nombre not in existentes:
                tag = TagORM(nombre=nombre)
                self._session.add(tag)
                nuevos.append(tag)

        if nuevos:
            self._session.flush()   # asigna IDs a los nuevos

        return list(existentes.values()) + nuevos

    def listar_todos(self) -> list[TagORM]:
        return list(self._session.scalars(select(TagORM)).all())

    def obtener_por_nombre(self, nombre: str) -> Optional[TagORM]:
        stmt = select(TagORM).where(TagORM.nombre == nombre.lower().strip())
        return self._session.scalars(stmt).one_or_none()


# reportes
class ReporteRepository:
    """
    Gestiona la persistencia de reportes y su reconstrucción completa
    como objetos de dominio (incluyendo todas sus interacciones).

    Depende de `UsuarioRepository` y `TagRepository` para resolver las
    relaciones al rehidratar objetos de dominio.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._usuario_repo = UsuarioRepository(session)
        self._tag_repo = TagRepository(session)

    # --- Helpers internos ---

    def _cargar_usuarios_para_reporte(self, r_orm: ReporteORM) -> dict[int, Usuario]:
        """
        Carga en un diccionario todos los usuarios involucrados en un
        reporte (autor + autores de interacciones). Esto evita queries
        N+1 al reconstruir el dominio.
        """
        ids_necesarios = {r_orm.autor_id} | {i.autor_id for i in r_orm.interacciones}
        stmt = select(UsuarioORM).where(UsuarioORM.id.in_(ids_necesarios))
        return {
            u.id: _usuario_orm_a_dominio(u)
            for u in self._session.scalars(stmt).all()
        }

    # --- CRUD principal ---

    def guardar(self, reporte: Reporte) -> Reporte:
        """
        Persiste un nuevo reporte. El objeto de dominio `reporte` debe
        tener `autor.id` ya establecido (el autor debe existir en la BD).

        Determina automáticamente el `tipo_reporte` según la clase
        concreta de `reporte`.
        """
        tipo = _CLASE_DOMINIO_A_TIPO_REPORTE.get(type(reporte))
        if tipo is None:
            raise TypeError(f"Tipo de reporte no registrado: {type(reporte)}")

        tags_orm = self._tag_repo.obtener_o_crear(set(reporte.tags))
        r_orm = _reporte_dominio_a_orm(reporte, tipo, tags_orm)
        self._session.add(r_orm)
        self._session.flush()
        reporte._id = r_orm.id
        return reporte

    def obtener_por_id(self, reporte_id: int) -> Optional[Reporte]:
        """
        Carga el reporte completo (con interacciones y tags mediante
        eager loading declarado en el ORM con `lazy='selectin'`) y
        lo reconstituye como objeto de dominio.
        """
        r_orm = self._session.get(ReporteORM, reporte_id)
        if r_orm is None:
            return None
        cache = self._cargar_usuarios_para_reporte(r_orm)
        return _reporte_orm_a_dominio(r_orm, cache)

    def actualizar_estado_forzado(self, reporte: Reporte) -> None:
        """
        Persiste el `_estado_forzado` cuando un moderador archiva un
        reporte. Solo actualiza ese campo para no sobreescribir el
        resto de la fila innecesariamente.
        """
        stmt = select(ReporteORM).where(ReporteORM.id == reporte.id)
        r_orm = self._session.scalars(stmt).one_or_none()
        if r_orm is None:
            raise EntidadNoEncontrada(f"Reporte id={reporte.id} no encontrado.")
        r_orm.estado_forzado = reporte._estado_forzado

    # --- Queries de búsqueda y filtrado ---

    def listar_activos(self) -> list[Reporte]:
        """
        Devuelve todos los reportes cuyo `estado_forzado` no sea
        ARCHIVADO. El estado dinámico (EXPIRADO, etc.) se filtra
        al acceder a `reporte.estado` en el objeto de dominio:
        aquí traemos todo y dejamos que el dominio decida.

        Nota: para escalar se podría filtrar en SQL por `timestamp`
        reciente, pero el enunciado no exige optimización avanzada.
        """
        stmt = select(ReporteORM).where(
            ReporteORM.estado_forzado != EstadoReporte.ARCHIVADO
        )
        return self._reconstituir_lista(self._session.scalars(stmt).all())

    def listar_todos(self) -> list[Reporte]:
        """Devuelve todos los reportes sin filtrar (útil para admin)."""
        stmt = select(ReporteORM)
        return self._reconstituir_lista(self._session.scalars(stmt).all())

    def buscar_por_tags(self, tags: set[str]) -> list[Reporte]:
        """
        Devuelve reportes que tengan AL MENOS UNO de los tags dados.
        Se une con la tabla de asociación `reporte_tags` y filtra
        por nombre de tag.
        """
        tags_lower = {t.lower().strip() for t in tags}
        stmt = (
            select(ReporteORM)
            .join(reporte_tags, ReporteORM.id == reporte_tags.c.reporte_id)
            .join(TagORM, TagORM.id == reporte_tags.c.tag_id)
            .where(TagORM.nombre.in_(tags_lower))
            .distinct()
        )
        return self._reconstituir_lista(self._session.scalars(stmt).all())

    def buscar_por_edificio(self, edificio: str) -> list[Reporte]:
        """Filtra reportes por nombre de edificio (case-insensitive)."""
        stmt = select(ReporteORM).where(
            func.lower(ReporteORM.ubicacion_edificio) == edificio.lower()
        )
        return self._reconstituir_lista(self._session.scalars(stmt).all())

    def buscar_por_zona_geografica(
        self,
        lat_min: float, lat_max: float,
        lon_min: float, lon_max: float,
    ) -> list[Reporte]:
        """
        Filtra reportes dentro de un bounding box geográfico.
        Solo considera reportes que tengan latitud/longitud registradas.
        """
        stmt = select(ReporteORM).where(
            ReporteORM.ubicacion_lat.isnot(None),
            ReporteORM.ubicacion_lon.isnot(None),
            ReporteORM.ubicacion_lat.between(lat_min, lat_max),
            ReporteORM.ubicacion_lon.between(lon_min, lon_max),
        )
        return self._reconstituir_lista(self._session.scalars(stmt).all())

    def buscar_por_autor(self, autor_id: int) -> list[Reporte]:
        """Devuelve todos los reportes de un usuario específico."""
        stmt = select(ReporteORM).where(ReporteORM.autor_id == autor_id)
        return self._reconstituir_lista(self._session.scalars(stmt).all())

    def buscar_por_tipo(self, tipo_reporte: str) -> list[Reporte]:
        """Filtra por tipo de reporte (ej: 'alerta_emergencia')."""
        stmt = select(ReporteORM).where(ReporteORM.tipo_reporte == tipo_reporte)
        return self._reconstituir_lista(self._session.scalars(stmt).all())

    def feed_ordenado(self, limite: int = 50) -> list[Reporte]:
        """
        Devuelve los reportes más recientes (no archivados) para el feed
        principal, ordenados por timestamp descendente. El ordenamiento
        por prioridad se hace en memoria después de reconstituir, ya que
        `calcular_prioridad()` vive en el dominio.
        """
        stmt = (
            select(ReporteORM)
            .where(ReporteORM.estado_forzado.is_(None))   # excluye archivados/forzados
            .order_by(ReporteORM.timestamp.desc())
            .limit(limite * 2)  # traemos más para filtrar expirados y re-ordenar
        )
        reportes = self._reconstituir_lista(self._session.scalars(stmt).all())
        activos = [r for r in reportes if r.esta_activo]
        activos.sort(key=lambda r: r.calcular_prioridad(), reverse=True)
        return activos[:limite]

    # --- Helper de reconstrucción en bulk ---

    def _reconstituir_lista(self, orms: list[ReporteORM]) -> list[Reporte]:
        """
        Reconstruye una lista de reportes de dominio desde sus ORMs,
        cargando todos los usuarios necesarios en una sola query
        (evita N+1 al iterar sobre múltiples reportes).
        """
        if not orms:
            return []

        # Recolectar todos los IDs de usuario involucrados
        ids_usuarios: set[int] = set()
        for r_orm in orms:
            ids_usuarios.add(r_orm.autor_id)
            for i in r_orm.interacciones:
                ids_usuarios.add(i.autor_id)

        stmt = select(UsuarioORM).where(UsuarioORM.id.in_(ids_usuarios))
        cache: dict[int, Usuario] = {
            u.id: _usuario_orm_a_dominio(u)
            for u in self._session.scalars(stmt).all()
        }

        return [_reporte_orm_a_dominio(r_orm, cache) for r_orm in orms]


# =============================================================================
# REPOSITORIO DE INTERACCIONES
# =============================================================================

class InteraccionRepository:
    """
    Persiste nuevas interacciones (comentarios, votos, denuncias) y
    sincroniza los efectos colaterales (cambio de reputación del autor
    del reporte) de vuelta a la BD.

    En la mayoría de los flujos, esto se llama DESPUÉS de que el dominio
    ya aplicó la interacción en memoria (ej: `reporte.confirmar(usuario)`).
    El repositorio solo se ocupa de la persistencia del resultado.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._usuario_repo = UsuarioRepository(session)

    def guardar(
        self,
        interaccion: Interaccion,
        reporte_id: int,
    ) -> None:
        """
        Persiste la interacción y sincroniza el puntaje de reputación
        del autor del reporte (que el dominio ya modificó en memoria).
        """
        i_orm = _interaccion_dominio_a_orm(interaccion, reporte_id)
        self._session.add(i_orm)
        self._session.flush()
        interaccion._id = i_orm.id   # sincronizar ID al objeto dominio

    def listar_por_reporte(self, reporte_id: int) -> list[InteraccionORM]:
        """Devuelve los ORM de interacciones (para endpoints de lectura)."""
        stmt = (
            select(InteraccionORM)
            .where(InteraccionORM.reporte_id == reporte_id)
            .order_by(InteraccionORM.timestamp)
        )
        return list(self._session.scalars(stmt).all())


# suscripciones
class SuscripcionRepository:
    """Persiste las suscripciones (un usuario sigue un tag o un edificio)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def _a_dominio(s_orm: SuscripcionORM) -> Suscripcion:
        return Suscripcion(
            id=s_orm.id,
            usuario_id=s_orm.usuario_id,
            tipo=TipoSuscripcion(s_orm.tipo),
            valor=s_orm.valor,
        )

    def crear(self, usuario_id: int, tipo: TipoSuscripcion, valor: str) -> Suscripcion:
        """
        Crea (o devuelve, si ya existe) la suscripción. Es idempotente: la
        restricción única de la BD garantiza que no haya duplicados.
        """
        valor = valor.strip().lower()
        existente = self._session.scalars(
            select(SuscripcionORM).where(
                SuscripcionORM.usuario_id == usuario_id,
                SuscripcionORM.tipo == tipo.value,
                SuscripcionORM.valor == valor,
            )
        ).one_or_none()
        if existente is not None:
            return self._a_dominio(existente)

        s_orm = SuscripcionORM(usuario_id=usuario_id, tipo=tipo.value, valor=valor)
        self._session.add(s_orm)
        self._session.flush()
        return self._a_dominio(s_orm)

    def listar_por_usuario(self, usuario_id: int) -> list[Suscripcion]:
        stmt = select(SuscripcionORM).where(SuscripcionORM.usuario_id == usuario_id)
        return [self._a_dominio(s) for s in self._session.scalars(stmt).all()]

    def listar_todas(self) -> list[Suscripcion]:
        """Todas las suscripciones (usado para resolver a quién notificar)."""
        return [self._a_dominio(s) for s in self._session.scalars(select(SuscripcionORM)).all()]

    def eliminar(self, suscripcion_id: int, usuario_id: int) -> bool:
        """Elimina una suscripción propia. Devuelve True si existía y era del usuario."""
        s_orm = self._session.get(SuscripcionORM, suscripcion_id)
        if s_orm is None or s_orm.usuario_id != usuario_id:
            return False
        self._session.delete(s_orm)
        return True


# notis
class NotificacionRepository:
    """Persiste y consulta las notificaciones de los usuarios."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def _a_dominio(n_orm: NotificacionORM) -> Notificacion:
        return Notificacion(
            id=n_orm.id,
            usuario_id=n_orm.usuario_id,
            reporte_id=n_orm.reporte_id,
            mensaje=n_orm.mensaje,
            leida=n_orm.leida,
            timestamp=_as_utc(n_orm.timestamp),
        )

    def guardar(self, notificacion: Notificacion) -> Notificacion:
        n_orm = NotificacionORM(
            usuario_id=notificacion.usuario_id,
            reporte_id=notificacion.reporte_id,
            mensaje=notificacion.mensaje,
            leida=notificacion.leida,
        )
        self._session.add(n_orm)
        self._session.flush()
        return self._a_dominio(n_orm)

    def listar_por_usuario(
        self, usuario_id: int, solo_no_leidas: bool = False
    ) -> list[Notificacion]:
        stmt = select(NotificacionORM).where(NotificacionORM.usuario_id == usuario_id)
        if solo_no_leidas:
            stmt = stmt.where(NotificacionORM.leida.is_(False))
        stmt = stmt.order_by(NotificacionORM.timestamp.desc())
        return [self._a_dominio(n) for n in self._session.scalars(stmt).all()]

    def contar_no_leidas(self, usuario_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(NotificacionORM)
            .where(
                NotificacionORM.usuario_id == usuario_id,
                NotificacionORM.leida.is_(False),
            )
        )
        return int(self._session.scalar(stmt) or 0)

    def marcar_leida(self, notificacion_id: int, usuario_id: int) -> bool:
        n_orm = self._session.get(NotificacionORM, notificacion_id)
        if n_orm is None or n_orm.usuario_id != usuario_id:
            return False
        n_orm.leida = True
        return True

    def marcar_todas_leidas(self, usuario_id: int) -> int:
        stmt = select(NotificacionORM).where(
            NotificacionORM.usuario_id == usuario_id,
            NotificacionORM.leida.is_(False),
        )
        notifs = self._session.scalars(stmt).all()
        for n in notifs:
            n.leida = True
        return len(notifs)


# unit of work
class UnitOfWork:
    """
    Agrupa todos los repositorios bajo una misma sesión/transacción.
    El patrón Unit of Work garantiza que un conjunto de operaciones
    (ej: guardar reporte + guardar interacciones + actualizar reputación)
    se ejecuten atómicamente: si algo falla, todo hace rollback.

    Uso típico (en la capa de servicios / FastAPI):

        with UnitOfWork(session_factory) as uow:
            autor = uow.usuarios.obtener_por_id(user_id)
            reporte = ReporteInfraestructura(autor=autor, ...)
            autor_registrado = uow.usuarios.guardar_nuevo(...)  # si es nuevo
            uow.reportes.guardar(reporte)
            uow.commit()

    Si se lanza una excepción dentro del bloque `with`, el __exit__
    llama a rollback automáticamente.
    """

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def __enter__(self) -> "UnitOfWork":
        self._session: Session = self._session_factory()
        self.usuarios = UsuarioRepository(self._session)
        self.reportes = ReporteRepository(self._session)
        self.interacciones = InteraccionRepository(self._session)
        self.tags = TagRepository(self._session)
        self.suscripciones = SuscripcionRepository(self._session)
        self.notificaciones = NotificacionRepository(self._session)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type:
            self._session.rollback()
        self._session.close()

    def commit(self) -> None:
        """Confirma todos los cambios de la sesión actual."""
        self._session.commit()

    def rollback(self) -> None:
        """Deshace todos los cambios de la sesión actual."""
        self._session.rollback()


# demo
if __name__ == "__main__":
    from app.persistence import crear_engine_sqlite, crear_tablas, crear_session_factory
    from app.domain import Estudiante, Moderador, ReporteInfraestructura, Ubicacion

    engine = crear_engine_sqlite("campusradar_repo_demo.db", echo=False)
    crear_tablas(engine)
    SessionFactory = crear_session_factory(engine)

    # ---- Registrar usuarios ----
    ana = Estudiante(nombre="Ana", email="ana@uchile.cl", password_hash="hash_ana")
    luis = Estudiante(nombre="Luis", email="luis@uchile.cl", password_hash="hash_luis")
    pedro = Moderador(nombre="Pedro", email="pedro@uchile.cl", password_hash="hash_pedro")

    with UnitOfWork(SessionFactory) as uow:
        uow.usuarios.guardar(ana)
        uow.usuarios.guardar(luis)
        uow.usuarios.guardar(pedro)
        uow.commit()

    print(f"Usuarios creados -> Ana id={ana.id}, Luis id={luis.id}, Pedro id={pedro.id}")

    # ---- Crear reporte ----
    ubic = Ubicacion(edificio="Hall Sur", piso=2, zona="Pasillo central",
                     latitud=-33.4572, longitud=-70.6642)
    reporte = ReporteInfraestructura(
        autor=ana,
        descripcion="El microondas del piso 2 no enciende.",
        ubicacion=ubic,
        tags={"infraestructura", "microondas", "hall-sur"},
    )

    with UnitOfWork(SessionFactory) as uow:
        uow.reportes.guardar(reporte)
        uow.commit()

    print(f"Reporte creado -> id={reporte.id}, estado={reporte.estado.value}")

    # ---- Confirmar reporte (Luis) ----
    with UnitOfWork(SessionFactory) as uow:
        reporte_cargado = uow.reportes.obtener_por_id(reporte.id)
        luis_cargado = uow.usuarios.obtener_por_id(luis.id)

        confirmacion = reporte_cargado.confirmar(luis_cargado)

        uow.interacciones.guardar(confirmacion, reporte_id=reporte_cargado.id)
        uow.usuarios.actualizar(reporte_cargado.autor)  # sincronizar reputación de Ana
        uow.commit()

    print(f"Confirmación guardada -> confirmacion id={confirmacion.id}")

    # ---- Releer y verificar estado ----
    with UnitOfWork(SessionFactory) as uow:
        r = uow.reportes.obtener_por_id(reporte.id)
        print(f"Estado tras recarga: {r.estado.value}")
        print(f"Confirmaciones: {r.total_confirmaciones}")
        print(f"Reputación de Ana: {r.autor.reputacion}")

    # ---- Búsquedas ----
    with UnitOfWork(SessionFactory) as uow:
        por_tag = uow.reportes.buscar_por_tags({"microondas"})
        print(f"Reportes con tag 'microondas': {len(por_tag)}")

        por_edificio = uow.reportes.buscar_por_edificio("Hall Sur")
        print(f"Reportes en 'Hall Sur': {len(por_edificio)}")

        feed = uow.reportes.feed_ordenado(limite=10)
        print(f"Feed principal ({len(feed)} reportes activos)")