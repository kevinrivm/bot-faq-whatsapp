"""Configuración del bot. Todo por variables de entorno, nada en el código.

Cada campo se documenta en `.env.example`. Los defaults permiten importar el
módulo sin entorno (los tests inyectan valores explícitos); lo obligatorio se
valida al arrancar, no a media conversación con un cliente esperando.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


def identidad_canonica(wa_id: str) -> str:
    """Canonicaliza un número de WhatsApp para comparar.

    México: Meta manda a veces `521XXXXXXXXXX` (13 dígitos, con el "1" de
    móvil) y a veces `52XXXXXXXXXX` — es la misma persona. Sin esto, un mismo
    contacto abre dos conversaciones y la pausa se aplica solo a una de ellas.
    """
    s = (wa_id or "").strip()
    if s.startswith("521") and len(s) == 13 and s.isdigit():
        return "52" + s[3:]
    return s


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- WhatsApp Cloud API -------------------------------------------------
    wa_phone_number_id: str = ""
    wa_token: str = ""
    wa_graph_version: str = "v25.0"

    # Segmento secreto de la URL del webhook y token del handshake de Meta.
    # El segmento en la ruta es la primera defensa: sin él, el endpoint es
    # público y cualquiera que adivine el dominio puede inyectar mensajes.
    webhook_token: str = ""
    # Firma de Meta (X-Hub-Signature-256). Vacío = NO se verifica, que es el
    # estado por defecto de muchas instalaciones. Ponerlo es gratis y cierra
    # el hueco: sin firma, el segmento secreto es lo único que protege.
    meta_app_secret: str = ""

    # --- Cerebro ------------------------------------------------------------
    llm_api_key: str = ""
    llm_model: str = "z-ai/glm-5.3-flash"
    llm_base_url: str = "https://openrouter.ai/api/v1"
    # Sin valor = defaults del proveedor. Ver `.env.example`: un max_tokens
    # bajo con un modelo que razona deja la respuesta VACÍA, no corta.
    llm_temperature: float | None = None
    llm_max_tokens: int | None = None

    # --- Conocimiento -------------------------------------------------------
    # Markdown con lo que el bot puede afirmar. Es su única fuente de verdad.
    conocimiento_path: str = "conocimiento/colegio-psicologos.md"
    nombre_agente: str = "el canal informativo"
    zona_horaria: str = "America/Mexico_City"

    # --- Comportamiento -----------------------------------------------------
    # Segundos que espera para juntar una ráfaga ("hola" / "oye" / "una
    # pregunta") en un solo turno. Sin esto contesta tres veces a lo mismo.
    coalesce_segundos: float = 4.0
    # Horas de silencio tras las que la IA se reactiva sola después de que un
    # humano tomó la conversación.
    reactivar_tras_horas: float = 24.0
    historial_mensajes: int = 10
    # Modo prueba: solo responde a estas identidades (CSV). Vacío = a todos.
    numeros_permitidos: str = ""

    database_url: str = ""
    port: int = 8000

    @property
    def identidades_permitidas(self) -> frozenset[str]:
        return frozenset(
            identidad_canonica(p) for p in self.numeros_permitidos.split(",") if p.strip()
        )

    @property
    def muestreo(self) -> dict[str, object]:
        """Lo que SÍ se configuró. Vacío = defaults del proveedor."""
        crudo = {"temperature": self.llm_temperature, "max_tokens": self.llm_max_tokens}
        return {k: v for k, v in crudo.items() if v is not None}

    def faltantes(self) -> list[str]:
        """Lo obligatorio que no está. Se revisa al arrancar."""
        requeridos = {
            "WA_PHONE_NUMBER_ID": self.wa_phone_number_id,
            "WA_TOKEN": self.wa_token,
            "WEBHOOK_TOKEN": self.webhook_token,
            "LLM_API_KEY": self.llm_api_key,
            "DATABASE_URL": self.database_url,
        }
        return [k for k, v in requeridos.items() if not str(v).strip()]
