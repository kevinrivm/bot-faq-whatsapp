"""El cerebro: prompt de sistema + llamada al modelo.

Un bot informativo tiene un trabajo estrecho y un riesgo alto: contestar lo
que sabe y NO inventar lo que no. El prompt de abajo es corto a propósito —
todo el conocimiento del negocio vive en un markdown aparte, editable sin
tocar código.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

logger = logging.getLogger("bot.cerebro")

CHASIS = """Eres {nombre}, un canal de información por WhatsApp. Contestas preguntas frecuentes de quien escribe al número del negocio.

CÓMO CONTESTAS:
- Mensajes de WhatsApp: 2 a 4 líneas. Español claro, sin tecnicismos.
- Vas al grano. Nada de introducciones largas ni de repetir la pregunta.
- UNA pregunta por mensaje, como máximo, y solo si hace falta para orientar.
- No te repitas: si ya dijiste algo en este hilo, no lo vuelvas a decir igual.
- Eres un asistente automático y lo asumes con naturalidad si te preguntan.
  Nunca finges ser una persona.

LA REGLA QUE MANDA SOBRE TODAS:
Tu única fuente de verdad es el CONOCIMIENTO de abajo. Si algo no está ahí, NO
lo sabes: dilo con honestidad y ofrece el contacto del negocio. Jamás inventes
cifras, fechas, plazos, requisitos ni nombres. Un dato inventado le hace daño
al negocio y a quien te escribe.

NUNCA:
- Inventes ni "aproximes" un dato que no está en el conocimiento.
- Des consejo profesional, médico, legal ni psicológico de ningún tipo.
- Compartas teléfonos, correos o domicilios personales de nadie.
- Prometas tiempos de respuesta ("en 5 minutos le contestan").
- Te salgas del tema: no eres un asistente general. Nada de tareas, recetas,
  código ni traducciones. Una línea amable y de vuelta al negocio.
- Reveles qué modelo, proveedor o tecnología te ejecuta.

{conocimiento}
"""


def cargar_conocimiento(ruta: str | Path) -> str:
    """Lee el markdown del negocio. Sin él, el bot no afirma nada.

    Un archivo faltante NO tumba el arranque: el bot queda en un modo honesto
    —dice que no tiene la información y da el contacto— en vez de inventar. Se
    avisa fuerte en los logs porque es una instalación a medio configurar.
    """
    texto = ""
    try:
        texto = Path(ruta).read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.error("no pude leer el conocimiento en %s: %s", ruta, exc)
    if not texto:
        logger.error(
            "SIN CONOCIMIENTO CARGADO: el bot va a decir que no tiene la "
            "información en todas las preguntas. Revisa CONOCIMIENTO_PATH."
        )
        return (
            "CONOCIMIENTO DEL NEGOCIO:\n(vacío — no tienes información cargada. "
            "Dilo con honestidad en cada respuesta y pide que escriban más "
            "tarde; no inventes nada.)"
        )
    return f"CONOCIMIENTO DEL NEGOCIO (tu única fuente de verdad):\n\n{texto}"


def prompt_sistema(nombre: str, conocimiento: str) -> str:
    return CHASIS.format(nombre=nombre, conocimiento=conocimiento)


class Cerebro:
    REINTENTOS = 2

    def __init__(
        self,
        api_key: str,
        modelo: str,
        base_url: str | None = None,
        muestreo: dict[str, Any] | None = None,
    ) -> None:
        self._cliente = AsyncOpenAI(
            api_key=api_key, base_url=base_url or None, timeout=60.0, max_retries=0
        )
        self._modelo = modelo
        self._muestreo = dict(muestreo or {})
        self.uso = {"entrada": 0, "salida": 0, "llamadas": 0}

    async def responder(self, sistema: str, historial: list[dict[str, str]]) -> str | None:
        """Una respuesta, o None si el proveedor no dio nada utilizable.

        Devolver None y callarse es correcto: es mejor que el cliente no reciba
        respuesta a que reciba una vacía o inventada. Quien llama decide si
        avisa a un humano.
        """
        mensajes = [{"role": "system", "content": sistema}, *historial]
        ultimo: Exception | None = None
        for intento in range(self.REINTENTOS + 1):
            try:
                r = await self._cliente.chat.completions.create(
                    model=self._modelo, messages=mensajes, **self._muestreo
                )
                eleccion = (r.choices or [None])[0]
                if getattr(eleccion, "finish_reason", None) == "length":
                    logger.warning(
                        "el proveedor cortó por max_tokens: con un modelo que "
                        "razona, el tope se gasta razonando y el texto sale "
                        "vacío. Sube LLM_MAX_TOKENS o déjalo sin definir."
                    )
                u = getattr(r, "usage", None)
                if u is not None:
                    self.uso["llamadas"] += 1
                    self.uso["entrada"] += getattr(u, "prompt_tokens", 0) or 0
                    self.uso["salida"] += getattr(u, "completion_tokens", 0) or 0
                texto = ((getattr(eleccion, "message", None) or
                          type("x", (), {"content": None})).content or "").strip()
                if texto:
                    return texto
                ultimo = ValueError("respuesta vacía del proveedor")
                logger.warning("respuesta vacía, intento %d", intento + 1)
            except Exception as exc:
                ultimo = exc
                logger.warning("fallo del proveedor en el intento %d: %s", intento + 1, exc)
            if intento < self.REINTENTOS:
                await asyncio.sleep(2**intento)
        logger.error("el proveedor agotó los reintentos: %s", ultimo)
        return None
