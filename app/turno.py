"""El turno: juntar la ráfaga, decidir si contestar, contestar.

Aquí vive la regla que pidió el cliente: si una persona contesta desde el
teléfono, la IA se calla; y vuelve sola cuando el hilo lleva 24 h en silencio.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.cerebro import Cerebro, prompt_sistema
from app.config import Settings, identidad_canonica
from app.db import Almacen, debe_reactivarse
from app.meta import ClienteWhatsApp

logger = logging.getLogger("bot.turno")


@dataclass
class Contexto:
    settings: Settings
    almacen: Almacen
    wa: ClienteWhatsApp
    cerebro: Cerebro
    conocimiento: str
    pendientes: dict[str, asyncio.Task] = field(default_factory=dict)


async def encolar(ctx: Contexto, identidad: str) -> None:
    """Arranca (o reinicia) el temporizador de la ráfaga.

    Quien escribe "hola", "oye" y "una pregunta" en cinco segundos mandó UN
    turno, no tres. Cada mensaje nuevo reinicia la espera; cuando pasa el
    silencio, se contesta una sola vez a todo junto.
    """
    tarea = ctx.pendientes.get(identidad)
    if tarea and not tarea.done():
        tarea.cancel()
    ctx.pendientes[identidad] = asyncio.create_task(_esperar_y_responder(ctx, identidad))


async def _esperar_y_responder(ctx: Contexto, identidad: str) -> None:
    try:
        await asyncio.sleep(ctx.settings.coalesce_segundos)
        await responder(ctx, identidad)
    except asyncio.CancelledError:
        pass  # llegó otro mensaje: el turno lo cierra el temporizador nuevo
    except Exception:
        logger.exception("turno de %s: fallo no controlado", identidad)
    finally:
        ctx.pendientes.pop(identidad, None)


async def _puede_contestar(ctx: Contexto, identidad: str) -> bool:
    """¿La IA tiene permiso de hablar en este hilo?

    Tres candados, en orden: la allowlist del modo prueba, la pausa por
    intervención humana, y la reactivación por silencio.
    """
    permitidas = ctx.settings.identidades_permitidas
    if permitidas and identidad not in permitidas:
        logger.info("%s no está en la allowlist: no se responde", identidad)
        return False

    conv = await ctx.almacen.conversacion(identidad)
    if conv is None or conv["pausada_en"] is None:
        return True

    if debe_reactivarse(
        conv["pausada_en"],
        conv["ultimo_mensaje_en"],
        ctx.settings.reactivar_tras_horas,
    ):
        await ctx.almacen.reactivar(identidad)
        logger.info(
            "%s: la IA se reactiva sola (%.0f h sin actividad en el hilo)",
            identidad,
            ctx.settings.reactivar_tras_horas,
        )
        return True

    logger.info("%s: en pausa (%s), la IA no contesta", identidad, conv["pausada_por"])
    return False


async def responder(ctx: Contexto, identidad: str) -> str | None:
    """Cierra el turno: piensa una respuesta y la manda."""
    if not await _puede_contestar(ctx, identidad):
        return None

    filas = await ctx.almacen.historial(identidad, ctx.settings.historial_mensajes)
    historial = [
        {
            "role": "user" if f["direccion"] == "entrante" else "assistant",
            "content": f["texto"],
        }
        for f in filas
        if f["texto"]
    ]
    if not historial:
        return None

    sistema = prompt_sistema(ctx.settings.nombre_agente, ctx.conocimiento)
    texto = await ctx.cerebro.responder(sistema, historial)
    if not texto:
        # Callarse es la degradación correcta: mejor sin respuesta que con una
        # inventada. Queda en los logs para que se vea al revisar.
        logger.error("%s: sin respuesta del proveedor, el turno se cierra mudo", identidad)
        return None

    enviado = await ctx.wa.enviar(identidad, texto)
    if enviado:
        await ctx.almacen.registrar_mensaje(
            enviado, identidad, "saliente", texto, origen="ia"
        )
        await ctx.almacen.tocar(identidad)
    return texto


async def procesar_entrante(ctx: Contexto, mensaje: dict) -> None:
    """Un mensaje del cliente: se guarda y se programa la respuesta."""
    from app.meta import texto_de

    origen_id = mensaje.get("id") or ""
    identidad = identidad_canonica(mensaje.get("from") or "")
    if not origen_id or not identidad:
        return

    await ctx.almacen.asegurar_conversacion(identidad, mensaje.get("_nombre"))
    texto = texto_de(mensaje)

    nuevo = await ctx.almacen.registrar_mensaje(
        origen_id, identidad, "entrante", texto, origen="lead"
    )
    if not nuevo:
        logger.info("mensaje %s repetido (reintento de Meta): ignorado", origen_id)
        return
    await ctx.almacen.tocar(identidad)
    await ctx.wa.marcar_leido(origen_id)

    if texto is None:
        # Una foto o un audio a un bot informativo: no se ignora en silencio,
        # se dice con honestidad. Callarse parece que el negocio no contesta.
        tipo = mensaje.get("type", "ese tipo de mensaje")
        aviso = (
            "Por este canal solo puedo leer mensajes de texto — no alcanzo a "
            f"revisar {tipo}. ¿Me lo escribe y con gusto le oriento?"
        )
        enviado = await ctx.wa.enviar(identidad, aviso)
        if enviado:
            await ctx.almacen.registrar_mensaje(
                enviado, identidad, "saliente", aviso, origen="ia"
            )
        return

    await encolar(ctx, identidad)


async def procesar_echo(ctx: Contexto, echo: dict) -> None:
    """Un mensaje que el negocio mandó desde el teléfono: pausa la IA.

    Es la señal que pidió el cliente. Meta la manda cuando el número está en
    Coexistence (Cloud API + la app de WhatsApp en el celular). En cuanto una
    persona contesta a mano, el bot se aparta: nada peor que la IA hablando
    encima de quien ya está atendiendo.
    """
    crudo = echo.get("raw") or {}
    identidad = identidad_canonica(echo.get("para") or crudo.get("to") or "")
    if not identidad:
        return

    from app.meta import texto_de

    await ctx.almacen.asegurar_conversacion(identidad, None)
    mensaje_id = crudo.get("id") or ""
    if mensaje_id:
        await ctx.almacen.registrar_mensaje(
            mensaje_id, identidad, "saliente", texto_de(crudo), origen="humano"
        )

    # Si había un turno en camino, se cancela: la persona ya contestó.
    tarea = ctx.pendientes.pop(identidad, None)
    if tarea and not tarea.done():
        tarea.cancel()

    await ctx.almacen.pausar(identidad, por="humano")
    logger.info(
        "%s: contestaron desde el teléfono — IA en pausa, vuelve tras %.0f h "
        "de silencio",
        identidad,
        ctx.settings.reactivar_tras_horas,
    )


async def barrer_pausas(ctx: Contexto) -> int:
    """Reactiva las conversaciones que ya cumplieron su silencio.

    No es imprescindible —`_puede_contestar` ya reactiva cuando llega un
    mensaje— pero deja el estado limpio y hace que la pantalla de una consulta
    a la base diga la verdad sin tener que interpretarla.
    """
    filas = await ctx.almacen.pool.fetch(
        "SELECT identidad, pausada_en, ultimo_mensaje_en FROM conversaciones "
        "WHERE pausada_en IS NOT NULL"
    )
    ahora = datetime.now(timezone.utc)
    reactivadas = 0
    for f in filas:
        if debe_reactivarse(
            f["pausada_en"], f["ultimo_mensaje_en"],
            ctx.settings.reactivar_tras_horas, ahora,
        ):
            await ctx.almacen.reactivar(f["identidad"])
            reactivadas += 1
    if reactivadas:
        logger.info("barrido: %d conversación(es) devueltas a la IA", reactivadas)
    return reactivadas
