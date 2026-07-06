"""
CampusRadar - Paso 1: Modelado de Dominio (POO Puro)
======================================================

Este módulo contiene las clases de dominio puras, sin dependencias de
frameworks (FastAPI) ni de persistencia (SQLAlchemy). Representan el
"corazón" del negocio: Usuarios, Reportes y sus Interacciones.

DECISIONES DE DISEÑO CLAVE
---------------------------

1. Abstracción / Herencia (Reportes):
   `Reporte` es una clase abstracta (ABC) que define el contrato común
   (atributos, ciclo de vida, interacciones). Las subclases concretas
   (`ReporteInfraestructura`, `ReporteAlertaEmergencia`, etc.) solo
   especializan comportamiento puntual (ej: criterios de expiración,
   nivel de prioridad) mediante polimorfismo.

2. Ciclo de vida con Lazy Evaluation:
   El estado de un reporte NO se actualiza con un scheduler/timer.
   En su lugar, el atributo `estado` es una *property* calculada: cada
   vez que se accede (`reporte.estado`), se ejecuta `_calcular_estado()`,
   que evalúa la antigüedad (vía `_ahora()`), el balance de
   confirmaciones/desmentidos y la reputación de los participantes.
   Esto evita time.sleep, hilos o loops infinitos: el estado "se
   materializa" solo cuando alguien lo necesita (al consultarlo,
   serializarlo, etc.).

3. Encapsulamiento:
   Los atributos críticos (listas de interacciones, contadores) son
   privados (`_confirmaciones`, `_desmentidos`, etc.) y se exponen
   mediante properties de solo lectura o métodos controlados
   (`confirmar()`, `desmentir()`, `comentar()`, `denunciar()`), que
   son los únicos puntos de entrada para modificar el estado interno.
   Esto garantiza invariantes (ej: un usuario no puede confirmar y
   desmentir el mismo reporte simultáneamente).

4. Composición:
   - `Reporte` se compone de `Ubicacion`, listas de `Interaccion`
     (Comentario, Confirmacion, Desmentido, Denuncia) y `Usuario` (autor).
   - `Usuario` se compone de un objeto `Reputacion` que encapsula la
     lógica de puntaje y nivel de credibilidad, en lugar de tener un
     simple entero disperso en la clase.

5. Polimorfismo:
   - `calcular_prioridad()` y `tiempo_de_vida_base()` son métodos
     abstractos/sobrescritos por cada subclase de Reporte, permitiendo
     que cada tipo tenga su propia lógica de expiración y prioridad
     sin que el código cliente necesite conocer el tipo concreto
     (se itera sobre `list[Reporte]` indistintamente).
   - `Interaccion` es abstracta; `Confirmacion`, `Desmentido`,
     `Comentario` y `Denuncia` sobrescriben `aplicar_efecto()`, que
     define cómo cada interacción impacta el reporte y la reputación
     del autor.

6. Jerarquía de Usuarios:
   `Usuario` (abstracta) -> `Estudiante`, `Moderador`, `Administrador`.
   Cada nivel sobrescribe `permisos()` (polimorfismo) devolviendo el
   conjunto de `Permiso` que posee. Los moderadores/administradores
   heredan y extienden los permisos de estudiante mediante `super()`.

7. Enums para estados/tipos:
   Se usan `Enum` para `EstadoReporte`, `Permiso`, `NivelReputacion` y
   `TipoInteraccion`, evitando "strings mágicos" y facilitando la
   validación y el mapeo futuro a columnas de base de datos.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from typing import Optional
import itertools


def _ahora() -> datetime:
    """
    Instante actual como datetime *timezone-aware* en UTC.

    Se usa en todo el dominio (en vez de `datetime.now()` naive) para ser
    consistente con las columnas `DateTime(timezone=True)` de la base de datos.
    En PostgreSQL esas columnas devuelven datetimes aware; mezclarlos con naive
    provoca `TypeError: can't subtract offset-naive and offset-aware datetimes`
    y, peor aún, desfases de zona horaria en el ciclo de vida (p.ej. la
    expiración a 2h de una alerta). Usar UTC aware de punta a punta lo evita.
    """
    return datetime.now(timezone.utc)


#enums
class EstadoReporte(Enum):
    """Estados posibles del ciclo de vida de un reporte."""
    NUEVO = "nuevo"
    VERIFICADO = "verificado"
    CONTROVERTIDO = "controvertido"
    CRITICO = "critico"
    EXPIRADO = "expirado"
    ARCHIVADO = "archivado"


class TipoInteraccion(Enum):
    COMENTARIO = "comentario"
    CONFIRMACION = "confirmacion"
    DESMENTIDO = "desmentido"
    DENUNCIA = "denuncia"


class NivelReputacion(Enum):
    """Niveles cualitativos derivados del puntaje numérico de reputación."""
    NUEVO = "nuevo"            # poca o ninguna trayectoria
    CONFIABLE = "confiable"
    DESTACADO = "destacado"
    BAJA_CREDIBILIDAD = "baja_credibilidad"


class Permiso(Enum):
    """Permisos granulares del sistema (capability-based)."""
    CREAR_REPORTE = auto()
    COMENTAR = auto()
    CONFIRMAR = auto()
    DESMENTIR = auto()
    DENUNCIAR = auto()
    MODERAR_CONTENIDO = auto()      # ocultar/archivar reportes denunciados
    GESTIONAR_USUARIOS = auto()     # banear, cambiar roles, etc.
    VER_PANEL_ADMIN = auto()


#reputacion
class Reputacion:
    """
    Encapsula el puntaje de credibilidad de un usuario.

    Se usa composición (en vez de heredar de int) para poder controlar
    las reglas de incremento/decremento y exponer un nivel cualitativo
    (`NivelReputacion`) que otras partes del sistema pueden consultar
    sin conocer los detalles numéricos.
    """

    PUNTAJE_INICIAL = 50
    PUNTAJE_MINIMO = 0
    PUNTAJE_MAXIMO = 100

    # Umbrales para niveles cualitativos
    UMBRAL_DESTACADO = 80
    UMBRAL_CONFIABLE = 40
    UMBRAL_BAJA_CREDIBILIDAD = 15

    def __init__(self, puntaje: int = PUNTAJE_INICIAL) -> None:
        self._puntaje = max(self.PUNTAJE_MINIMO, min(self.PUNTAJE_MAXIMO, puntaje))

    @property
    def puntaje(self) -> int:
        return self._puntaje

    @property
    def nivel(self) -> NivelReputacion:
        """Calcula el nivel cualitativo en base al puntaje actual (lazy)."""
        if self._puntaje >= self.UMBRAL_DESTACADO:
            return NivelReputacion.DESTACADO
        if self._puntaje >= self.UMBRAL_CONFIABLE:
            return NivelReputacion.CONFIABLE
        if self._puntaje <= self.UMBRAL_BAJA_CREDIBILIDAD:
            return NivelReputacion.BAJA_CREDIBILIDAD
        return NivelReputacion.NUEVO

    @property
    def peso_voto(self) -> float:
        """
        Peso que tiene el voto (confirmación/desmentido) de este usuario
        al evaluar el ciclo de vida de un reporte. Usuarios con más
        reputación "pesan más" en la balanza.
        """
        if self.nivel == NivelReputacion.DESTACADO:
            return 2.0
        if self.nivel == NivelReputacion.BAJA_CREDIBILIDAD:
            return 0.5
        return 1.0

    def ajustar(self, delta: int) -> None:
        """Incrementa o decrementa el puntaje, respetando los límites."""
        self._puntaje = max(self.PUNTAJE_MINIMO, min(self.PUNTAJE_MAXIMO, self._puntaje + delta))

    def __repr__(self) -> str:
        return f"Reputacion(puntaje={self._puntaje}, nivel={self.nivel.value})"


#jerarquia de usuarios
class Usuario(ABC):
    """
    Clase abstracta base para todos los actores del sistema.

    Encapsula identidad, credenciales (hash de contraseña, NO la
    contraseña en texto plano) y reputación. La jerarquía concreta
    (`Estudiante`, `Moderador`, `Administrador`) define el conjunto de
    permisos vía polimorfismo (`permisos()`).
    """

    _id_counter = itertools.count(1)

    def __init__(self, nombre: str, email: str, password_hash: str) -> None:
        self._id = next(Usuario._id_counter)
        self._nombre = nombre
        self._email = email
        self._password_hash = password_hash  # nunca se guarda texto plano
        self._reputacion = Reputacion()
        self._activo = True
        self._fecha_registro = _ahora()

    # encapsulamiento
    @property
    def id(self) -> int:
        return self._id

    @property
    def nombre(self) -> str:
        return self._nombre

    @property
    def email(self) -> str:
        return self._email

    @property
    def reputacion(self) -> Reputacion:
        return self._reputacion

    @property
    def activo(self) -> bool:
        return self._activo

    def desactivar(self) -> None:
        """Permite banear/desactivar una cuenta (usado por moderación)."""
        self._activo = False

    def verificar_password(self, password_hash_intento: str) -> bool:
        """
        Compara el hash recibido contra el almacenado.
        El hashing real (bcrypt/argon2, etc.) se delega a la capa de
        infraestructura; aquí solo se compara.
        """
        return self._password_hash == password_hash_intento

    # --- Polimorfismo: cada rol define su propio set de permisos ---
    @abstractmethod
    def permisos(self) -> set[Permiso]:
        """Retorna el conjunto de permisos otorgados a este rol."""
        raise NotImplementedError

    def tiene_permiso(self, permiso: Permiso) -> bool:
        return self._activo and permiso in self.permisos()

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(id={self._id}, nombre={self._nombre!r})"


class Estudiante(Usuario):
    """Rol base: puede crear contenido e interactuar."""

    def permisos(self) -> set[Permiso]:
        return {
            Permiso.CREAR_REPORTE,
            Permiso.COMENTAR,
            Permiso.CONFIRMAR,
            Permiso.DESMENTIR,
            Permiso.DENUNCIAR,
        }


class Moderador(Estudiante):
    """
    Hereda todos los permisos de Estudiante y agrega capacidades de
    moderación de contenido. Se reutiliza `super().permisos()` para
    extender en vez de redefinir desde cero (evita duplicación).
    """

    def permisos(self) -> set[Permiso]:
        return super().permisos() | {Permiso.MODERAR_CONTENIDO}


class Administrador(Moderador):
    """Máximo nivel: agrega gestión de usuarios y panel administrativo."""

    def permisos(self) -> set[Permiso]:
        return super().permisos() | {
            Permiso.GESTIONAR_USUARIOS,
            Permiso.VER_PANEL_ADMIN,
        }


#ubicacion
@dataclass(frozen=True)
class Ubicacion:
    """
    Value Object inmutable que representa la ubicación aproximada de un
    reporte dentro del campus. Se usa `frozen=True` porque una ubicación
    no debería mutar una vez asociada a un reporte (si cambia, se crea
    una nueva instancia).
    """
    edificio: str
    piso: Optional[int] = None
    zona: Optional[str] = None          # descripción libre / referencia en mapa
    latitud: Optional[float] = None
    longitud: Optional[float] = None

    def __str__(self) -> str:
        partes = [self.edificio]
        if self.piso is not None:
            partes.append(f"piso {self.piso}")
        if self.zona:
            partes.append(self.zona)
        return " - ".join(partes)


# interacciones
class Interaccion(ABC):
    """
    Clase abstracta para cualquier interacción de un usuario sobre un
    reporte. Cada subclase implementa `aplicar_efecto(reporte)`, que
    encapsula cómo esa interacción específica impacta:
      - el contador correspondiente del reporte, y
      - la reputación del autor del reporte (no de quien interactúa).

    Este diseño permite agregar nuevos tipos de interacción sin tocar
    la clase `Reporte` (Open/Closed Principle): basta crear una nueva
    subclase de `Interaccion`.
    """

    _id_counter = itertools.count(1)

    def __init__(self, autor: Usuario) -> None:
        self._id = next(Interaccion._id_counter)
        self._autor = autor
        self._timestamp = _ahora()

    @property
    def id(self) -> int:
        return self._id

    @property
    def autor(self) -> Usuario:
        return self._autor

    @property
    def timestamp(self) -> datetime:
        return self._timestamp

    @property
    @abstractmethod
    def tipo(self) -> TipoInteraccion:
        ...

    @abstractmethod
    def aplicar_efecto(self, reporte: "Reporte") -> None:
        """
        Aplica el efecto de esta interacción sobre el reporte dado
        (ej: incrementar contadores, ajustar reputación del autor
        del reporte). Llamado por `Reporte` al registrar la interacción.
        """
        raise NotImplementedError


class Comentario(Interaccion):
    """Interacción textual sin impacto directo en métricas de validación."""

    def __init__(self, autor: Usuario, texto: str) -> None:
        super().__init__(autor)
        self._texto = texto

    @property
    def texto(self) -> str:
        return self._texto

    @property
    def tipo(self) -> TipoInteraccion:
        return TipoInteraccion.COMENTARIO

    def aplicar_efecto(self, reporte: "Reporte") -> None:
        # Los comentarios no alteran métricas de validación ni reputación.
        pass


class Confirmacion(Interaccion):
    """Voto positivo: el reporte parece verídico."""

    @property
    def tipo(self) -> TipoInteraccion:
        return TipoInteraccion.CONFIRMACION

    def aplicar_efecto(self, reporte: "Reporte") -> None:
        # Pequeño incentivo de reputación al autor del reporte por
        # cada confirmación recibida, ponderado por la reputación
        # de quien confirma.
        incremento = max(1, round(1 * self.autor.reputacion.peso_voto))
        reporte.autor.reputacion.ajustar(incremento)


class Desmentido(Interaccion):
    """Voto negativo: el reporte parece falso o ya no vigente."""

    @property
    def tipo(self) -> TipoInteraccion:
        return TipoInteraccion.DESMENTIDO

    def aplicar_efecto(self, reporte: "Reporte") -> None:
        decremento = max(1, round(1 * self.autor.reputacion.peso_voto))
        reporte.autor.reputacion.ajustar(-decremento)


class Denuncia(Interaccion):
    """Marca el reporte como potencialmente inapropiado/erróneo para moderación."""

    def __init__(self, autor: Usuario, motivo: str) -> None:
        super().__init__(autor)
        self._motivo = motivo

    @property
    def motivo(self) -> str:
        return self._motivo

    @property
    def tipo(self) -> TipoInteraccion:
        return TipoInteraccion.DENUNCIA

    def aplicar_efecto(self, reporte: "Reporte") -> None:
        # Penalización leve inmediata; la moderación humana decide la
        # sanción definitiva. Esto solo "marca" el reporte.
        reporte.autor.reputacion.ajustar(-1)


#reporte
class Reporte(ABC):
    """
    Entidad central del dominio. Es abstracta: no se instancia
    directamente, sino a través de sus subclases concretas (cada una
    representando un "tipo de publicación" del enunciado).

    CICLO DE VIDA / LAZY EVALUATION
    --------------------------------
    El atributo público `estado` es una *property*. Internamente se
    guarda `_estado_forzado` (Optional[EstadoReporte]) que permite a
    un moderador fijar manualmente un estado (ej: ARCHIVADO a la
    fuerza). Si no hay estado forzado, `estado` delega en
    `_calcular_estado()`, que:

      1. Si fue archivado manualmente -> ARCHIVADO (estado terminal).
      2. Si superó su tiempo de vida (`tiempo_de_vida_base()`,
         polimórfico por subclase) -> EXPIRADO.
      3. Si el balance de votos ponderado es muy negativo -> CRITICO
         (posible información falsa/peligrosa).
      4. Si hay desmentidos y confirmaciones simultáneos y reñidos ->
         CONTROVERTIDO.
      5. Si tiene suficientes confirmaciones ponderadas -> VERIFICADO.
      6. En cualquier otro caso -> NUEVO.

    No hay ningún hilo/timer corriendo: cada vez que algo (la API, un
    test, la UI) pregunta `reporte.estado`, se recalcula con la hora
    actual (`_ahora()`), lo cual es la esencia de la evaluación
    perezosa solicitada en el enunciado.
    """

    # Umbrales configurables (podrían moverse a config en el futuro)
    UMBRAL_VERIFICADO = 3.0      # confirmaciones ponderadas netas
    UMBRAL_CRITICO = -3.0        # desmentidos ponderados netos
    MARGEN_CONTROVERSIA = 1.5    # diferencia máxima para considerar "reñido"

    _id_counter = itertools.count(1)

    def __init__(
        self,
        autor: Usuario,
        descripcion: str,
        ubicacion: Ubicacion,
        tags: Optional[set[str]] = None,
    ) -> None:
        if not autor.tiene_permiso(Permiso.CREAR_REPORTE):
            raise PermissionError(f"{autor} no tiene permiso para crear reportes.")

        self._id = next(Reporte._id_counter)
        self._autor = autor
        self._descripcion = descripcion
        self._ubicacion = ubicacion
        self._tags: set[str] = set(tags or set())
        self._timestamp = _ahora()

        # Colecciones encapsuladas de interacciones
        self._comentarios: list[Comentario] = []
        self._confirmaciones: list[Confirmacion] = []
        self._desmentidos: list[Desmentido] = []
        self._denuncias: list[Denuncia] = []

        # Conjunto de usuarios que ya votaron (confirmar/desmentir),
        # para evitar votos duplicados -> garantiza invariante de negocio.
        self._usuarios_que_votaron: set[int] = set()

        # Estado forzado manualmente (ej: archivado por un moderador).
        # Si es None, el estado se calcula dinámicamente (lazy).
        self._estado_forzado: Optional[EstadoReporte] = None

    # solo lectura
    @property
    def id(self) -> int:
        return self._id

    @property
    def autor(self) -> Usuario:
        return self._autor

    @property
    def descripcion(self) -> str:
        return self._descripcion

    @property
    def ubicacion(self) -> Ubicacion:
        return self._ubicacion

    @property
    def tags(self) -> frozenset[str]:
        return frozenset(self._tags)

    @property
    def timestamp(self) -> datetime:
        return self._timestamp

    @property
    def antiguedad(self) -> timedelta:
        """Tiempo transcurrido desde la creación, calculado al vuelo."""
        return _ahora() - self._timestamp

    @property
    def comentarios(self) -> tuple[Comentario, ...]:
        return tuple(self._comentarios)

    @property
    def total_confirmaciones(self) -> int:
        return len(self._confirmaciones)

    @property
    def total_desmentidos(self) -> int:
        return len(self._desmentidos)

    @property
    def total_denuncias(self) -> int:
        return len(self._denuncias)

    def agregar_tags(self, *tags: str) -> None:
        self._tags.update(t.lower().strip() for t in tags)

    # hooks
    @abstractmethod
    def tiempo_de_vida_base(self) -> timedelta:
        """
        Tiempo de vida "natural" del reporte antes de considerarse
        EXPIRADO (en ausencia de otros factores). Cada subclase define
        un valor acorde a su naturaleza (ej: una alerta de emergencia
        expira mucho más rápido que info de infraestructura).
        """
        raise NotImplementedError

    @abstractmethod
    def calcular_prioridad(self) -> int:
        """
        Retorna un nivel de prioridad (mayor = más urgente), usado por
        ranking/feed. Cada subclase pondera distinto sus propios
        factores (ej: alertas de emergencia siempre altas).
        """
        raise NotImplementedError

    # balance de votos
    def _balance_ponderado(self) -> float:
        """
        Suma los pesos de voto de quienes confirmaron, menos la suma de
        pesos de quienes desmintieron. Usuarios con mayor reputación
        "pesan" más (ver `Reputacion.peso_voto`).
        """
        positivo = sum(c.autor.reputacion.peso_voto for c in self._confirmaciones)
        negativo = sum(d.autor.reputacion.peso_voto for d in self._desmentidos)
        return positivo - negativo

    # lazy evaluation
    def _calcular_estado(self) -> EstadoReporte:
        # 1. Estado terminal forzado (ej. archivado manualmente).
        if self._estado_forzado is not None:
            return self._estado_forzado

        # 2. Expiración por antigüedad (polimórfica según el tipo).
        if self.antiguedad > self.tiempo_de_vida_base():
            return EstadoReporte.EXPIRADO

        balance = self._balance_ponderado()

        # 3. Balance muy negativo -> posible info falsa/peligrosa.
        if balance <= self.UMBRAL_CRITICO:
            return EstadoReporte.CRITICO

        # 4. Votos reñidos en ambas direcciones -> controvertido.
        hay_ambos = self._confirmaciones and self._desmentidos
        if hay_ambos and abs(balance) < self.MARGEN_CONTROVERSIA:
            return EstadoReporte.CONTROVERTIDO

        # 5. Suficiente respaldo positivo -> verificado.
        if balance >= self.UMBRAL_VERIFICADO:
            return EstadoReporte.VERIFICADO

        # 6. Caso base: recién creado / sin suficiente señal.
        return EstadoReporte.NUEVO

    @property
    def estado(self) -> EstadoReporte:
        """Punto de acceso público y perezoso al estado actual."""
        return self._calcular_estado()

    @property
    def esta_activo(self) -> bool:
        """
        Determina, también de forma perezosa, si el reporte sigue
        siendo "relevante" para mostrarse destacado en el feed.
        EXPIRADO y ARCHIVADO se consideran inactivos.
        """
        return self.estado not in (EstadoReporte.EXPIRADO, EstadoReporte.ARCHIVADO)

    # --- Acciones de moderación ---
    def archivar(self, moderador: Usuario) -> None:
        """
        Fuerza el estado a ARCHIVADO. Solo usuarios con permiso de
        moderación pueden hacerlo (encapsulamiento de la regla).
        """
        if not moderador.tiene_permiso(Permiso.MODERAR_CONTENIDO):
            raise PermissionError(f"{moderador} no puede archivar reportes.")
        self._estado_forzado = EstadoReporte.ARCHIVADO

    # registro
    def _registrar_interaccion(self, interaccion: Interaccion) -> None:
        """
        Punto único interno para añadir una interacción a la colección
        correspondiente y aplicar su efecto. Centralizar aquí permite
        agregar lógica transversal futura (ej: notificaciones) sin
        duplicar código en cada método público.
        """
        if isinstance(interaccion, Comentario):
            self._comentarios.append(interaccion)
        elif isinstance(interaccion, Confirmacion):
            self._confirmaciones.append(interaccion)
        elif isinstance(interaccion, Desmentido):
            self._desmentidos.append(interaccion)
        elif isinstance(interaccion, Denuncia):
            self._denuncias.append(interaccion)
        else:
            raise TypeError(f"Tipo de interacción no soportado: {type(interaccion)}")

        interaccion.aplicar_efecto(self)

    def comentar(self, autor: Usuario, texto: str) -> Comentario:
        if not autor.tiene_permiso(Permiso.COMENTAR):
            raise PermissionError(f"{autor} no puede comentar.")
        comentario = Comentario(autor=autor, texto=texto)
        self._registrar_interaccion(comentario)
        return comentario

    def _validar_voto_unico(self, autor: Usuario) -> None:
        if autor.id == self._autor.id:
            raise ValueError("Un usuario no puede votar su propio reporte.")
        if autor.id in self._usuarios_que_votaron:
            raise ValueError(f"{autor} ya emitió un voto sobre este reporte.")

    def confirmar(self, autor: Usuario) -> Confirmacion:
        if not autor.tiene_permiso(Permiso.CONFIRMAR):
            raise PermissionError(f"{autor} no puede confirmar reportes.")
        self._validar_voto_unico(autor)
        confirmacion = Confirmacion(autor=autor)
        self._registrar_interaccion(confirmacion)
        self._usuarios_que_votaron.add(autor.id)
        return confirmacion

    def desmentir(self, autor: Usuario) -> Desmentido:
        if not autor.tiene_permiso(Permiso.DESMENTIR):
            raise PermissionError(f"{autor} no puede desmentir reportes.")
        self._validar_voto_unico(autor)
        desmentido = Desmentido(autor=autor)
        self._registrar_interaccion(desmentido)
        self._usuarios_que_votaron.add(autor.id)
        return desmentido

    def denunciar(self, autor: Usuario, motivo: str) -> Denuncia:
        if not autor.tiene_permiso(Permiso.DENUNCIAR):
            raise PermissionError(f"{autor} no puede denunciar contenido.")
        denuncia = Denuncia(autor=autor, motivo=motivo)
        self._registrar_interaccion(denuncia)
        return denuncia

    def usuario_ya_voto(self, usuario_id: int) -> bool:
        """Indica si el usuario ya emitió una confirmación o desmentido sobre este reporte."""
        return usuario_id in self._usuarios_que_votaron

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(id={self._id}, estado={self.estado.value}, "
            f"ubicacion={self._ubicacion}, confirmaciones={self.total_confirmaciones}, "
            f"desmentidos={self.total_desmentidos})"
        )


#subclases
class ReporteInfraestructura(Reporte):
    """
    Ej: microondas roto, sala liberada, corte de agua/luz.
    Vida media: relativamente larga, ya que estas condiciones suelen
    persistir hasta que alguien las repare.
    """

    def tiempo_de_vida_base(self) -> timedelta:
        return timedelta(hours=12)

    def calcular_prioridad(self) -> int:
        # Prioridad media; sube si tiene desmentidos bajos y confirmaciones altas.
        base = 3
        return base + self.total_confirmaciones - self.total_desmentidos


class ReporteActividadExtraprogramatica(Reporte):
    """Ej: charla, taller, actividad cultural con comida/sobrante, etc."""

    def tiempo_de_vida_base(self) -> timedelta:
        return timedelta(hours=4)

    def calcular_prioridad(self) -> int:
        base = 2
        return base + self.total_confirmaciones


class ReporteAlertaEmergencia(Reporte):
    """
    Ej: incendio, sismo, situación de seguridad. Vida corta (la
    información debe ser muy fresca) pero prioridad máxima siempre.
    """

    def tiempo_de_vida_base(self) -> timedelta:
        return timedelta(hours=2)

    def calcular_prioridad(self) -> int:
        # Las alertas de emergencia siempre tienen prioridad máxima,
        # independiente de votos -> ejemplo de polimorfismo "extremo".
        return 100


class ReporteEventoUniversitario(Reporte):
    """Ej: feria, seminario, actividad masiva planificada."""

    def tiempo_de_vida_base(self) -> timedelta:
        return timedelta(days=1)

    def calcular_prioridad(self) -> int:
        base = 1
        return base + self.total_confirmaciones - (2 * self.total_desmentidos)


class ReporteInformacionLogistica(Reporte):
    """Ej: congestión de un sector, cambios de horario, rutas alternativas."""

    def tiempo_de_vida_base(self) -> timedelta:
        return timedelta(hours=6)

    def calcular_prioridad(self) -> int:
        base = 2
        return base + self.total_confirmaciones - self.total_desmentidos


# suscripciones y notificaciones
class TipoSuscripcion(Enum):
    """Criterio por el cual un usuario sigue contenido del campus."""
    TAG = "tag"
    EDIFICIO = "edificio"


@dataclass(frozen=True)
class Suscripcion:
    """
    Representa que un usuario "sigue" un tag o un edificio. Cuando se publica un
    reporte que coincide con la suscripción, se genera una `Notificacion`.

    Es un value object inmutable; `id` lo asigna la capa de persistencia. La
    regla de coincidencia (`coincide`) es lógica de dominio pura y testeable.
    """
    usuario_id: int
    tipo: TipoSuscripcion
    valor: str
    id: Optional[int] = None

    def coincide(self, reporte: "Reporte") -> bool:
        """¿El reporte dado cae dentro de esta suscripción?"""
        objetivo = self.valor.strip().lower()
        if self.tipo == TipoSuscripcion.TAG:
            return objetivo in {t.lower() for t in reporte.tags}
        if self.tipo == TipoSuscripcion.EDIFICIO:
            return objetivo == reporte.ubicacion.edificio.strip().lower()
        return False


class Notificacion:
    """
    Aviso dirigido a un usuario, generado porque un reporte coincidió con alguna
    de sus suscripciones. Encapsula su estado de lectura (`leida`), que solo se
    modifica mediante `marcar_leida()`.
    """

    def __init__(
        self,
        usuario_id: int,
        reporte_id: int,
        mensaje: str,
        leida: bool = False,
        id: Optional[int] = None,
        timestamp: Optional[datetime] = None,
    ) -> None:
        self._id = id
        self._usuario_id = usuario_id
        self._reporte_id = reporte_id
        self._mensaje = mensaje
        self._leida = leida
        self._timestamp = timestamp or _ahora()

    @property
    def id(self) -> Optional[int]:
        return self._id

    @property
    def usuario_id(self) -> int:
        return self._usuario_id

    @property
    def reporte_id(self) -> int:
        return self._reporte_id

    @property
    def mensaje(self) -> str:
        return self._mensaje

    @property
    def leida(self) -> bool:
        return self._leida

    @property
    def timestamp(self) -> datetime:
        return self._timestamp

    def marcar_leida(self) -> None:
        self._leida = True

    def __repr__(self) -> str:
        estado = "leída" if self._leida else "no leída"
        return f"Notificacion(id={self._id}, usuario={self._usuario_id}, estado={estado!r})"


# ejemplo de uso (no lo usa la api, pero lo agregams pa probar q vola)
if __name__ == "__main__":
    # Crear usuarios
    ana = Estudiante(nombre="Ana", email="ana@uchile.cl", password_hash="hash1")
    luis = Estudiante(nombre="Luis", email="luis@uchile.cl", password_hash="hash2")
    pedro_mod = Moderador(nombre="Pedro", email="pedro@uchile.cl", password_hash="hash3")

    # Crear un reporte de infraestructura
    ubic = Ubicacion(edificio="Hall Sur", piso=2, zona="Cerca del ascensor")
    reporte = ReporteInfraestructura(
        autor=ana,
        descripcion="El microondas del piso 2 no enciende.",
        ubicacion=ubic,
        tags={"infraestructura", "microondas"},
    )

    print(reporte)
    print("Estado inicial:", reporte.estado)

    # Luis confirma el reporte
    reporte.confirmar(luis)
    print("Tras confirmación de Luis:", reporte.estado)
    print("Reputación de Ana:", ana.reputacion)

    # Pedro (moderador) comenta
    reporte.comentar(pedro_mod, "Ya fue reportado a mantenimiento.")
    print("Comentarios:", [c.texto for c in reporte.comentarios])

    # Intento de doble voto (debería fallar)
    try:
        reporte.confirmar(luis)
    except ValueError as e:
        print("Error esperado:", e)
