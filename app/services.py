"""
CampusRadar - Capa de servicios (lógica de negocio de orquestación)
===================================================================

Esta capa coordina dominio + repositorios para casos de uso que abarcan varias
entidades, manteniéndolos fuera de las rutas de FastAPI (api.py) y del dominio
puro (domain.py). Es la "lógica de negocio" que el enunciado pide separar del
dominio, la persistencia y la presentación.

Hoy alberga la generación de notificaciones a partir de las suscripciones cuando
se publica un reporte.
"""

from __future__ import annotations

from app.domain import Reporte, Notificacion, Suscripcion
from app.repositories import UnitOfWork


class NotificacionService:
    """
    Genera notificaciones cuando se publica un reporte que coincide con las
    suscripciones de los usuarios (seguir un tag o un edificio).

    Recibe la `session_factory` (no una sesión) para abrir su propia unidad de
    trabajo transaccional, de modo que la fan-out de notificaciones sea atómica
    e independiente del guardado del reporte.
    """

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def notificar_nuevo_reporte(self, reporte: Reporte) -> int:
        """
        Crea una notificación por cada usuario (distinto del autor) que sigue un
        tag o el edificio del reporte. Devuelve cuántas notificaciones creó.

        Se notifica una sola vez por usuario aunque coincidan varias de sus
        suscripciones (p.ej. sigue el edificio y además un tag del reporte).
        """
        with UnitOfWork(self._session_factory) as uow:
            suscripciones = uow.suscripciones.listar_todas()

            # usuario_id -> primera suscripción que disparó la coincidencia
            destinatarios: dict[int, Suscripcion] = {}
            for s in suscripciones:
                if s.usuario_id == reporte.autor.id:
                    continue  # no notificar al propio autor
                if s.usuario_id in destinatarios:
                    continue
                if s.coincide(reporte):
                    destinatarios[s.usuario_id] = s

            for usuario_id, s in destinatarios.items():
                mensaje = (
                    f"Nuevo reporte que sigues ({s.tipo.value}: {s.valor}): "
                    f"{reporte.descripcion[:80]}"
                )
                uow.notificaciones.guardar(
                    Notificacion(
                        usuario_id=usuario_id,
                        reporte_id=reporte.id,
                        mensaje=mensaje,
                    )
                )
            uow.commit()
            return len(destinatarios)
