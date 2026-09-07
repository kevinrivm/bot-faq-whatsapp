"""Postgres: el estado de cada conversación y la pausa de la IA.

Lo único que este bot necesita recordar es quién escribió, qué se dijo y si
un humano tomó la conversación. Sin panel, sin pipeline, sin fichas.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg

logger = logging.getLogger("bot.db")


class Almacen:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def conectar(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=8)

    async def cerrar(self) -> None:
        if self._pool:
            await self._pool.close()

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("el almacén no está conectado")
        return self._pool

    async def migrar(self, carpeta: Path) -> None:
        """Aplica los .sql en orden. Idempotente: todo es CREATE IF NOT EXISTS.

        Corre al arrancar y no como Pre-Deployment Command de Coolify: ahí se
        duplicaría, porque el contenedor ya las aplica solo.
        """
        for archivo in sorted(carpeta.glob("*.sql")):
            await self.pool.execute(archivo.read_text(encoding="utf-8"))
            logger.info("migración aplicada: %s", archivo.name)

    # -- conversaciones ----------------------------------------------------

    async def asegurar_conversacion(self, identidad: str, nombre: str | None) -> None:
        await self.pool.execute(
            """
            INSERT INTO conversaciones (identidad, nombre)
                 VALUES ($1, $2)
            ON CONFLICT (identidad) DO UPDATE
                    SET nombre = COALESCE(EXCLUDED.nombre, conversaciones.nombre)
            """,
            identidad,
            nombre,
        )

    async def conversacion(self, identidad: str) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            "SELECT * FROM conversaciones WHERE identidad = $1", identidad
        )

    async def tocar(self, identidad: str) -> None:
        """Marca actividad en el hilo. Es el reloj de la reactivación."""
        await self.pool.execute(
            "UPDATE conversaciones SET ultimo_mensaje_en = now() WHERE identidad = $1",
            identidad,
        )

    async def pausar(self, identidad: str, por: str = "humano") -> None:
        await self.pool.execute(
            """
            UPDATE conversaciones
               SET pausada_en = now(), pausada_por = $2, ultimo_mensaje_en = now()
             WHERE identidad = $1
            """,
            identidad,
            por,
        )

    async def reactivar(self, identidad: str) -> None:
        await self.pool.execute(
            "UPDATE conversaciones SET pausada_en = NULL, pausada_por = NULL "
            "WHERE identidad = $1",
            identidad,
        )

    # -- mensajes ----------------------------------------------------------

    async def registrar_mensaje(
        self,
        mensaje_id: str,
        identidad: str,
        direccion: str,
        texto: str | None,
        origen: str = "lead",
    ) -> bool:
        """Guarda un mensaje. Devuelve False si ya estaba (webhook repetido).

        Meta reintenta el webhook cuando no recibe 200 a tiempo. Sin esta
        comprobación el bot contesta dos veces al mismo mensaje, y el cliente
        ve al negocio hablando solo.
        """
        fila = await self.pool.fetchrow(
            """
            INSERT INTO mensajes (id, identidad, direccion, origen, texto)
                 VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (id) DO NOTHING
              RETURNING id
            """,
            mensaje_id,
            identidad,
            direccion,
            origen,
            texto,
        )
        return fila is not None

    async def historial(self, identidad: str, limite: int) -> list[asyncpg.Record]:
        filas = await self.pool.fetch(
            """
            SELECT direccion, texto FROM mensajes
             WHERE identidad = $1 AND texto IS NOT NULL
             ORDER BY creado_en DESC
             LIMIT $2
            """,
            identidad,
            limite,
        )
        return list(reversed(filas))


def debe_reactivarse(
    pausada_en: datetime | None,
    ultimo_mensaje_en: datetime | None,
    horas: float,
    ahora: datetime | None = None,
) -> bool:
    """¿Toca devolverle la conversación a la IA?

    La regla es "24 h desde el ÚLTIMO mensaje del hilo", no desde la pausa: si
    la persona y el humano siguen conversando, el reloj se reinicia con cada
    mensaje y la IA no se mete en medio. Solo vuelve cuando el hilo lleva un
    día entero en silencio.
    """
    if pausada_en is None:
        return False
    referencia = ultimo_mensaje_en or pausada_en
    ahora = ahora or datetime.now(timezone.utc)
    return ahora - referencia >= timedelta(hours=horas)
