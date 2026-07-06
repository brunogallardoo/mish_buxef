"""
CampusRadar - Paso 2: Capa de Persistencia (SQLAlchemy 2.0)
=============================================================

Este módulo define los MODELOS ORM (mapeo objeto-relacional). NO son las
mismas clases que el dominio puro de `domain.py`; son su contraparte
"persistible". Mantenemos esta separación deliberadamente.

DECISIONES DE DISEÑO CLAVE
---------------------------

1. Separación Dominio vs. Persistencia (capas independientes):
   Las clases de `domain.py` (Usuario, Reporte, Reputacion, etc.) NO
   heredan de `Base` ni conocen SQLAlchemy. Aquí definimos un modelo
   ORM paralelo (`UsuarioORM`, `ReporteORM`, etc.) cuyo único trabajo
   es mapear a tablas. La traducción entre ambos mundos ocurrirá en la
   capa de repositorios (Paso 2b / Paso 3), vía "mappers".
   Ventaja: el dominio se puede testear con pytest puro, sin DB, y el
   ORM se puede cambiar (ej: a Tortoise/Django ORM) sin tocar reglas
   de negocio.

2. Herencia de tablas para Reportes -> "Single Table Inheritance" (STI):
   En lugar de crear una tabla separada por cada subtipo de reporte
   (Infraestructura, Alerta, Evento, etc.), usamos UNA tabla
   `reportes` con una columna discriminadora `tipo_reporte`. SQLAlchemy
   mapea automáticamente cada subclase ORM (`ReporteInfraestructuraORM`,
   etc.) según el valor de esa columna (`polymorphic_identity`).
   Justificación: los reportes comparten casi todos sus campos
   (autor, ubicación, tags, timestamp, interacciones) y difieren solo
   en comportamiento (que vive en el dominio, no en la BD). STI evita
   joins innecesarios y es más simple de migrar/consultar, cumpliendo
   "no se evaluará complejidad avanzada del modelo relacional, pero sí
   que funcione adecuadamente".

3. Herencia de tablas para Usuarios -> también Single Table Inheritance:
   Mismo argumento: Estudiante/Moderador/Administrador comparten
   columnas (email, password_hash, reputación) y solo difieren en
   `permisos()`, que es lógica pura de dominio. Una columna `rol`
   discrimina el tipo.

4. Relaciones (Composición -> Foreign Keys + relationship()):
   - `ReporteORM.autor` -> `UsuarioORM` (muchos reportes, un autor).
   - `ReporteORM.ubicacion` -> embebida como columnas propias
     (edificio, piso, zona, lat, lon) en la misma tabla `reportes`
     (un Value Object inmutable no necesita tabla propia: se mapea
     "inline" -> menos joins, más simple).
   - `ReporteORM.tags` -> tabla de asociación many-to-many
     `reporte_tags` <-> `TagORM`, porque un tag (ej: "infraestructura")
     es compartido por muchos reportes y se usa para búsqueda/filtrado.
   - `InteraccionORM` -> también usa Single Table Inheritance
     (Comentario/Confirmacion/Desmentido/Denuncia comparten autor,
     reporte, timestamp; difieren en `texto`/`motivo` opcionales y en
     el discriminador `tipo_interaccion`).

5. Reputación NO se persiste como objeto separado:
   `Reputacion` en el dominio es un Value Object calculado a partir de
   un entero. En la BD basta una columna `puntaje_reputacion` en
   `usuarios`; el resto (nivel, peso_voto) se recalcula en el dominio
   al reconstruir el objeto `Reputacion(puntaje=...)`.

6. Estado del reporte: NO se persiste un campo "estado" calculado.
   Solo se persiste `estado_forzado` (nullable), que corresponde
   exactamente a `_estado_forzado` del dominio (para soportar archivado
   manual). El estado dinámico (NUEVO/VERIFICADO/etc.) sigue siendo
   lazy y se recalcula en memoria al reconstruir el objeto de dominio
   con sus interacciones y timestamp, tal como en el Paso 1. Esto evita
   tener un campo desincronizado con la realidad ("estado" dependiente
   del tiempo no puede vivir cómodamente como columna estática).

7. Timestamps con timezone-aware UTC:
   Usamos `DateTime(timezone=True)` y `func.now()` para evitar
   ambigüedades de zona horaria entre servidor/clientes.

8. `Mapped` / `mapped_column` (estilo SQLAlchemy 2.0):
   Se usa la sintaxis moderna tipada (`Mapped[...]`), que ofrece mejor
   integración con type checkers y es la forma recomendada actual.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    ForeignKey,
    String,
    Text,
    Table,
    Column,
    Integer,
    DateTime,
    Float,
    Boolean,
    Enum as SAEnum,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)

# Reutilizamos los Enums del dominio para que los valores almacenados
# en la BD sean exactamente los mismos strings que usa domain.py
# (evita una capa extra de mapeo de constantes).
from app.domain import EstadoReporte, TipoInteraccion


# =========================================================================
# BASE DECLARATIVA
# =========================================================================

class Base(DeclarativeBase):
    """Clase base declarativa para todos los modelos ORM del sistema."""
    pass


# =========================================================================
# USUARIOS (Single Table Inheritance)
# =========================================================================

class RolUsuario:
    """
    Constantes para el discriminador de rol. Se usan strings simples
    (no Enum de Python) para que coincidan 1:1 con los nombres de las
    clases de dominio (`Estudiante`, `Moderador`, `Administrador`),
    facilitando el mapper en la capa de repositorios.
    """
    ESTUDIANTE = "estudiante"
    MODERADOR = "moderador"
    ADMINISTRADOR = "administrador"


class UsuarioORM(Base):
    """
    Tabla `usuarios`. Mapea la jerarquía Usuario/Estudiante/Moderador/
    Administrador del dominio mediante Single Table Inheritance,
    discriminada por la columna `rol`.
    """

    __tablename__ = "usuarios"

    id: Mapped[int] = mapped_column(primary_key=True)
    nombre: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    rol: Mapped[str] = mapped_column(String(30), nullable=False, default=RolUsuario.ESTUDIANTE)

    # Corresponde al campo interno `_puntaje` de Reputacion en el dominio.
    puntaje_reputacion: Mapped[int] = mapped_column(Integer, nullable=False, default=50)

    activo: Mapped[bool] = mapped_column(default=True, nullable=False)
    fecha_registro: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relaciones inversas (un usuario puede ser autor de muchos reportes
    # e interacciones). Se usan principalmente para conveniencia de
    # consulta, no para construir el grafo de dominio completo de una.
    reportes: Mapped[list["ReporteORM"]] = relationship(
        back_populates="autor",
        foreign_keys="ReporteORM.autor_id",
        cascade="all, delete-orphan",
    )
    interacciones: Mapped[list["InteraccionORM"]] = relationship(
        back_populates="autor",
        cascade="all, delete-orphan",
    )

    __mapper_args__ = {
        "polymorphic_identity": "usuario_base",
        "polymorphic_on": rol,
    }

    def __repr__(self) -> str:
        return f"<UsuarioORM id={self.id} email={self.email!r} rol={self.rol}>"


class EstudianteORM(UsuarioORM):
    """Subclase STI: comparte la tabla `usuarios`, rol='estudiante'."""
    __mapper_args__ = {"polymorphic_identity": RolUsuario.ESTUDIANTE}


class ModeradorORM(UsuarioORM):
    """Subclase STI: comparte la tabla `usuarios`, rol='moderador'."""
    __mapper_args__ = {"polymorphic_identity": RolUsuario.MODERADOR}


class AdministradorORM(UsuarioORM):
    """Subclase STI: comparte la tabla `usuarios`, rol='administrador'."""
    __mapper_args__ = {"polymorphic_identity": RolUsuario.ADMINISTRADOR}


# =========================================================================
# TAGS (Many-to-Many con Reportes)
# =========================================================================

class TagORM(Base):
    """
    Tabla `tags`. Un tag (ej: 'microondas', 'emergencia') puede estar
    asociado a múltiples reportes, y un reporte puede tener múltiples
    tags -> relación many-to-many vía tabla de asociación.
    """

    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    nombre: Mapped[str] = mapped_column(String(60), nullable=False, unique=True, index=True)

    def __repr__(self) -> str:
        return f"<TagORM {self.nombre!r}>"


# Tabla de asociación pura (no necesita su propia clase ORM porque no
# tiene columnas adicionales más allá de las dos FKs).
reporte_tags = Table(
    "reporte_tags",
    Base.metadata,
    Column("reporte_id", ForeignKey("reportes.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


# =========================================================================
# REPORTES (Single Table Inheritance)
# =========================================================================

class TipoReporte:
    """
    Constantes discriminadoras para `ReporteORM`. Cada valor corresponde
    1:1 a una subclase concreta de `Reporte` en domain.py, permitiendo
    reconstruir el objeto de dominio correcto desde la fila de BD.
    """
    INFRAESTRUCTURA = "infraestructura"
    ACTIVIDAD_EXTRAPROGRAMATICA = "actividad_extraprogramatica"
    ALERTA_EMERGENCIA = "alerta_emergencia"
    EVENTO_UNIVERSITARIO = "evento_universitario"
    INFORMACION_LOGISTICA = "informacion_logistica"


class ReporteORM(Base):
    """
    Tabla `reportes`. Núcleo del sistema. Usa Single Table Inheritance
    discriminada por `tipo_reporte` para representar las 5 subclases de
    `Reporte` definidas en el dominio.

    La Ubicacion (Value Object inmutable en el dominio) se mapea
    "inline" como columnas propias de esta tabla (edificio, piso, zona,
    latitud, longitud) en vez de una tabla separada: es un objeto
    pequeño, sin identidad propia, siempre 1:1 con el reporte.

    El campo `estado_forzado` es nullable y corresponde a
    `Reporte._estado_forzado` del dominio (None => el estado se calcula
    de forma perezosa; un valor => override manual, ej. ARCHIVADO).
    """

    __tablename__ = "reportes"

    id: Mapped[int] = mapped_column(primary_key=True)

    tipo_reporte: Mapped[str] = mapped_column(String(40), nullable=False, index=True)

    autor_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False)
    autor: Mapped["UsuarioORM"] = relationship(back_populates="reportes", foreign_keys=[autor_id])

    descripcion: Mapped[str] = mapped_column(Text, nullable=False)

    # --- Ubicación embebida (Value Object inline) ---
    ubicacion_edificio: Mapped[str] = mapped_column(String(120), nullable=False)
    ubicacion_piso: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ubicacion_zona: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ubicacion_lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ubicacion_lon: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Override manual de estado (None = calculado dinámicamente en dominio).
    estado_forzado: Mapped[Optional[EstadoReporte]] = mapped_column(
        SAEnum(EstadoReporte, name="estado_reporte_enum"), nullable=True
    )

    # --- Relaciones ---
    tags: Mapped[list["TagORM"]] = relationship(secondary=reporte_tags, lazy="selectin")

    interacciones: Mapped[list["InteraccionORM"]] = relationship(
        back_populates="reporte",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="InteraccionORM.timestamp",
    )

    __mapper_args__ = {
        "polymorphic_identity": "reporte_base",
        "polymorphic_on": tipo_reporte,
    }

    def __repr__(self) -> str:
        return f"<ReporteORM id={self.id} tipo={self.tipo_reporte} autor_id={self.autor_id}>"


class ReporteInfraestructuraORM(ReporteORM):
    __mapper_args__ = {"polymorphic_identity": TipoReporte.INFRAESTRUCTURA}


class ReporteActividadExtraprogramaticaORM(ReporteORM):
    __mapper_args__ = {"polymorphic_identity": TipoReporte.ACTIVIDAD_EXTRAPROGRAMATICA}


class ReporteAlertaEmergenciaORM(ReporteORM):
    __mapper_args__ = {"polymorphic_identity": TipoReporte.ALERTA_EMERGENCIA}


class ReporteEventoUniversitarioORM(ReporteORM):
    __mapper_args__ = {"polymorphic_identity": TipoReporte.EVENTO_UNIVERSITARIO}


class ReporteInformacionLogisticaORM(ReporteORM):
    __mapper_args__ = {"polymorphic_identity": TipoReporte.INFORMACION_LOGISTICA}


# =========================================================================
# INTERACCIONES (Single Table Inheritance)
# =========================================================================

class InteraccionORM(Base):
    """
    Tabla `interacciones`. Usa Single Table Inheritance discriminada por
    `tipo_interaccion` (reutilizando el Enum `TipoInteraccion` del
    dominio) para representar Comentario/Confirmacion/Desmentido/Denuncia.

    - `texto`: usado solo por Comentario.
    - `motivo`: usado solo por Denuncia.
    Ambos son nullable porque Confirmacion/Desmentido no los usan;
    alternativamente podría normalizarse en tablas separadas, pero dado
    que "no se evaluará complejidad avanzada del modelo relacional",
    se prioriza simplicidad.

    Restricción `UniqueConstraint` sobre (reporte_id, autor_id,
    tipo_interaccion) SOLO tendría sentido para Confirmacion/Desmentido
    (un usuario no puede votar dos veces). Esa regla de "voto único" ya
    está garantizada por el dominio (`_validar_voto_unico`), pero se
    documenta aquí como candidata a constraint adicional si se quisiera
    reforzar a nivel de BD (se deja comentada para no impedir múltiples
    comentarios/denuncias del mismo usuario).
    """

    __tablename__ = "interacciones"

    id: Mapped[int] = mapped_column(primary_key=True)

    tipo_interaccion: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    reporte_id: Mapped[int] = mapped_column(ForeignKey("reportes.id", ondelete="CASCADE"), nullable=False)
    reporte: Mapped["ReporteORM"] = relationship(back_populates="interacciones")

    autor_id: Mapped[int] = mapped_column(ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False)
    autor: Mapped["UsuarioORM"] = relationship(back_populates="interacciones")

    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Campos específicos de ciertos subtipos (nullable).
    texto: Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # Comentario
    motivo: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Denuncia

    __mapper_args__ = {
        "polymorphic_identity": "interaccion_base",
        "polymorphic_on": tipo_interaccion,
    }

    # Ejemplo de constraint opcional para reforzar "voto único" a nivel BD
    # (se deja documentado, no activo, ya que aplicaría solo a
    # confirmacion/desmentido y STI no permite constraints condicionales
    # fácilmente sin tablas separadas):
    #
    # __table_args__ = (
    #     UniqueConstraint("reporte_id", "autor_id", "tipo_interaccion",
    #                      name="uq_voto_unico_por_reporte"),
    # )

    def __repr__(self) -> str:
        return f"<InteraccionORM id={self.id} tipo={self.tipo_interaccion} reporte_id={self.reporte_id}>"


class ComentarioORM(InteraccionORM):
    __mapper_args__ = {"polymorphic_identity": TipoInteraccion.COMENTARIO.value}


class ConfirmacionORM(InteraccionORM):
    __mapper_args__ = {"polymorphic_identity": TipoInteraccion.CONFIRMACION.value}


class DesmentidoORM(InteraccionORM):
    __mapper_args__ = {"polymorphic_identity": TipoInteraccion.DESMENTIDO.value}


class DenunciaORM(InteraccionORM):
    __mapper_args__ = {"polymorphic_identity": TipoInteraccion.DENUNCIA.value}


# =========================================================================
# SUSCRIPCIONES Y NOTIFICACIONES
# =========================================================================

class SuscripcionORM(Base):
    """
    Tabla `suscripciones`: un usuario sigue un tag o un edificio. La restricción
    única evita duplicados (mismo usuario + tipo + valor).
    """

    __tablename__ = "suscripciones"

    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int] = mapped_column(
        ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tipo: Mapped[str] = mapped_column(String(20), nullable=False)   # 'tag' | 'edificio'
    valor: Mapped[str] = mapped_column(String(120), nullable=False)

    __table_args__ = (
        UniqueConstraint("usuario_id", "tipo", "valor", name="uq_suscripcion"),
    )

    def __repr__(self) -> str:
        return f"<SuscripcionORM u={self.usuario_id} {self.tipo}={self.valor!r}>"


class NotificacionORM(Base):
    """
    Tabla `notificaciones`: avisos generados para un usuario cuando un reporte
    coincide con alguna de sus suscripciones. `leida` indica si ya fue vista.
    """

    __tablename__ = "notificaciones"

    id: Mapped[int] = mapped_column(primary_key=True)
    usuario_id: Mapped[int] = mapped_column(
        ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reporte_id: Mapped[int] = mapped_column(
        ForeignKey("reportes.id", ondelete="CASCADE"), nullable=False
    )
    mensaje: Mapped[str] = mapped_column(Text, nullable=False)
    leida: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<NotificacionORM id={self.id} u={self.usuario_id} leida={self.leida}>"


# =========================================================================
# ENGINE / SESSION HELPERS
# =========================================================================

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session


from sqlalchemy.pool import NullPool

def crear_engine(database_url: str = "campusradar.db", echo: bool = False):
    url = database_url if "://" in database_url else f"sqlite:///{database_url}"
    is_sqlite = url.startswith("sqlite")
    connect_args = {"check_same_thread": False} if is_sqlite else {}

    kwargs = dict(echo=echo, connect_args=connect_args, pool_pre_ping=True)
    if not is_sqlite:
        # Evita conexiones reutilizadas y muertas entre invocaciones frías.
        kwargs["poolclass"] = NullPool

    return create_engine(url, **kwargs)


def crear_engine_sqlite(ruta_archivo: str = "campusradar.db", echo: bool = False):
    """Atajo retrocompatible para SQLite local (usado por las demos `__main__`)."""
    return crear_engine(ruta_archivo, echo=echo)


def crear_tablas(engine) -> None:
    """Crea todas las tablas definidas en `Base.metadata` si no existen."""
    Base.metadata.create_all(engine)


def crear_session_factory(engine) -> sessionmaker[Session]:
    """Retorna una factory de sesiones ligada al engine dado."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# =========================================================================
# DEMOSTRACIÓN RÁPIDA (no es parte de la API final)
# =========================================================================

if __name__ == "__main__":
    import os
    if os.path.exists("campusradar_demo.db"):
        os.remove("campusradar_demo.db")      # ← agrega esta línea
    
    engine = crear_engine_sqlite("campusradar_demo.db", echo=False)
    # ... resto del código
    
    crear_tablas(engine)
    SessionLocal = crear_session_factory(engine)

    with SessionLocal() as session:
        # Crear usuarios
        ana = EstudianteORM(nombre="Ana", email="ana@uchile.cl", password_hash="hash1")
        luis = EstudianteORM(nombre="Luis", email="luis@uchile.cl", password_hash="hash2")
        session.add_all([ana, luis])
        session.commit()

        # Crear tags
        tag_infra = TagORM(nombre="infraestructura")
        tag_microondas = TagORM(nombre="microondas")
        session.add_all([tag_infra, tag_microondas])
        session.commit()

        # Crear reporte de infraestructura
        reporte = ReporteInfraestructuraORM(
            autor_id=ana.id,
            descripcion="El microondas del piso 2 no enciende.",
            ubicacion_edificio="Hall Sur",
            ubicacion_piso=2,
            ubicacion_zona="Cerca del ascensor",
            tags=[tag_infra, tag_microondas],
        )
        session.add(reporte)
        session.commit()

        # Agregar una confirmación de Luis
        confirmacion = ConfirmacionORM(reporte_id=reporte.id, autor_id=luis.id)
        session.add(confirmacion)
        session.commit()

        # Releer desde la BD
        reporte_db = session.get(ReporteORM, reporte.id)
        print(reporte_db)
        print("Autor:", reporte_db.autor)
        print("Ubicación:", reporte_db.ubicacion_edificio, "piso", reporte_db.ubicacion_piso)
        print("Tags:", [t.nombre for t in reporte_db.tags])
        print("Interacciones:", reporte_db.interacciones)
        print("Tipo concreto cargado:", type(reporte_db).__name__)
