"""WhatsApp Cloud API: verificar lo que llega y mandar lo que sale.

Este bot es headless: no hay CRM detrás, así que el token de WhatsApp vive
aquí y las respuestas salen directo a la Graph API.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import httpx

logger = logging.getLogger("bot.meta")


def firma_valida(cuerpo: bytes, cabecera: str | None, secreto: str) -> bool:
    """Comprueba `X-Hub-Signature-256`.

    Sin `secreto` configurado devuelve True: es el estado de muchas
    instalaciones y no se le va a negar el servicio a Meta por eso. Pero el
    endpoint queda protegido solo por el segmento secreto de la URL, y eso se
    avisa al arrancar.
    """
    if not secreto:
        return True
    if not cabecera or not cabecera.startswith("sha256="):
        return False
    esperado = hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest()
    # compare_digest y no ==: comparar hashes con == filtra información por el
    # tiempo que tarda en fallar.
    return hmac.compare_digest(esperado, cabecera.removeprefix("sha256="))


def extraer(
    payload: dict[str, Any], phone_number_id: str | None = None
) -> tuple[list[dict], list[dict]]:
    """Saca del webhook los mensajes entrantes y los echoes.

    Devuelve (entrantes, echoes). Un *echo* es un mensaje que el negocio mandó
    desde la app de WhatsApp del teléfono: Meta lo reenvía cuando el número
    está en Coexistence. Es lo que permite saber que un humano tomó la
    conversación sin que nadie tenga que avisar.

    Con `phone_number_id` se descarta todo lo que no venga dirigido a NUESTRO
    número. Importa cuando la app de Meta es de un Tech Provider: esa app está
    suscrita a las WABAs de varios clientes y firma con un único secreto, así
    que ese secreto no se puede repartir entre microservicios. Sin firma, esta
    comprobación es la que impide que un payload ajeno —o inventado— acabe
    contestándose con la voz de este negocio.

    Tolerante a propósito: si Meta cambia el envoltorio, se ignora lo que no
    se entiende en vez de reventar el webhook — un webhook que devuelve 500 se
    reintenta y acaba en bucle.
    """
    entrantes: list[dict] = []
    echoes: list[dict] = []
    for entrada in payload.get("entry") or []:
        for cambio in entrada.get("changes") or []:
            valor = cambio.get("value") or {}
            campo = cambio.get("field") or ""
            if phone_number_id:
                nuestro = (valor.get("metadata") or {}).get("phone_number_id")
                if nuestro and nuestro != phone_number_id:
                    logger.warning(
                        "webhook dirigido a otro número (%s): descartado", nuestro
                    )
                    continue
            contactos = {
                c.get("wa_id"): (c.get("profile") or {}).get("name")
                for c in (valor.get("contacts") or [])
                if c.get("wa_id")
            }
            # Meta documenta el campo `message_echoes`; algunas cuentas lo
            # mandan dentro de `messages` con `smb_message_echoes` como field.
            crudos_echo = valor.get("message_echoes") or []
            if "echo" in campo and not crudos_echo:
                crudos_echo = valor.get("messages") or []
            for m in crudos_echo:
                echoes.append({"raw": m, "para": m.get("to")})
            if crudos_echo:
                continue
            for m in valor.get("messages") or []:
                m["_nombre"] = contactos.get(m.get("from"))
                entrantes.append(m)
    return entrantes, echoes


def texto_de(mensaje: dict[str, Any]) -> str | None:
    """El texto de un mensaje, o None si es un tipo que este bot no atiende.

    Un bot informativo no necesita ver imágenes ni oír audios; lo que sí
    necesita es NO quedarse callado ante ellos. Quien llama decide qué
    contestar cuando esto devuelve None.
    """
    tipo = mensaje.get("type")
    if tipo == "text":
        return ((mensaje.get("text") or {}).get("body") or "").strip() or None
    if tipo == "button":
        return ((mensaje.get("button") or {}).get("text") or "").strip() or None
    if tipo == "interactive":
        inter = mensaje.get("interactive") or {}
        for clave in ("button_reply", "list_reply"):
            if clave in inter:
                return (inter[clave].get("title") or "").strip() or None
    return None


class ClienteWhatsApp:
    def __init__(self, phone_number_id: str, token: str, version: str = "v25.0") -> None:
        self._url = f"https://graph.facebook.com/{version}/{phone_number_id}/messages"
        self._cab = {"Authorization": f"Bearer {token}"}
        self._http = httpx.AsyncClient(timeout=30.0)

    async def enviar(self, para: str, texto: str) -> str | None:
        """Manda un texto. Devuelve el id del mensaje, o None si falló.

        Un fallo aquí NO tumba el turno: se registra y la conversación sigue.
        Meta rechaza por mil motivos (ventana de 24 h cerrada, límite de tier,
        token vencido) y ninguno merece un 500 que haga a Meta reintentar el
        webhook entero.
        """
        cuerpo = {
            "messaging_product": "whatsapp",
            "to": para,
            "type": "text",
            "text": {"body": texto[:4096], "preview_url": False},
        }
        try:
            r = await self._http.post(self._url, headers=self._cab, json=cuerpo)
            if r.status_code >= 300:
                logger.error("meta rechazó el envío (%s): %s", r.status_code, r.text[:300])
                return None
            datos = r.json()
            return ((datos.get("messages") or [{}])[0]).get("id")
        except Exception as exc:
            logger.error("no pude enviar a %s: %s", para, exc)
            return None

    async def marcar_leido(self, mensaje_id: str) -> None:
        """La palomita azul. Cosmético, pero es lo que hace que el cliente
        sepa que del otro lado alguien está. Un fallo aquí no importa."""
        try:
            await self._http.post(
                self._url,
                headers=self._cab,
                json={
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": mensaje_id,
                },
            )
        except Exception:
            pass

    async def cerrar(self) -> None:
        await self._http.aclose()
