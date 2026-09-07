"""La regla del cliente: la IA se calla si contesta un humano, y vuelve sola.

Es la única lógica de negocio propia de este bot, así que es la que más
merece pruebas. Lo demás (webhook, envío) es cableado con Meta.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings, identidad_canonica
from app.db import debe_reactivarse
from app.meta import extraer, firma_valida, texto_de

AHORA = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


class TestReactivacion:
    def test_una_conversacion_sin_pausa_no_se_reactiva(self) -> None:
        assert debe_reactivarse(None, AHORA, 24, AHORA) is False

    def test_antes_de_las_24h_la_ia_sigue_callada(self) -> None:
        pausada = AHORA - timedelta(hours=30)
        ultimo = AHORA - timedelta(hours=23, minutes=59)
        assert debe_reactivarse(pausada, ultimo, 24, AHORA) is False

    def test_a_las_24h_de_silencio_la_ia_vuelve(self) -> None:
        pausada = AHORA - timedelta(hours=30)
        ultimo = AHORA - timedelta(hours=24)
        assert debe_reactivarse(pausada, ultimo, 24, AHORA) is True

    def test_el_reloj_cuenta_desde_el_ultimo_mensaje_no_desde_la_pausa(self) -> None:
        """Si el humano y la persona siguen hablando, la IA NO se mete.

        Contar desde la pausa haría que el bot irrumpiera a mitad de una
        conversación humana viva, que es justo lo que se quiere evitar.
        """
        pausada = AHORA - timedelta(days=5)
        ultimo = AHORA - timedelta(hours=1)  # siguen conversando
        assert debe_reactivarse(pausada, ultimo, 24, AHORA) is False

    def test_sin_actividad_registrada_se_cuenta_desde_la_pausa(self) -> None:
        pausada = AHORA - timedelta(hours=25)
        assert debe_reactivarse(pausada, None, 24, AHORA) is True


class TestIdentidad:
    def test_mexico_con_y_sin_el_uno_es_la_misma_persona(self) -> None:
        """Meta manda 521… y 52… para el mismo número. Sin canonicalizar, la
        pausa se aplicaría a una conversación y la respuesta saldría por la
        otra."""
        assert identidad_canonica("5214621349768") == "524621349768"
        assert identidad_canonica("524621349768") == "524621349768"

    def test_otros_paises_pasan_tal_cual(self) -> None:
        assert identidad_canonica("34600111222") == "34600111222"


class TestWebhook:
    def test_separa_entrantes_de_echoes(self) -> None:
        """El echo es lo que delata que un humano contestó desde el teléfono.
        Confundirlo con un mensaje entrante haría que el bot se contestara a
        sí mismo."""
        payload = {
            "entry": [{"changes": [{
                "field": "messages",
                "value": {
                    "contacts": [{"wa_id": "5214621349768",
                                  "profile": {"name": "Kevin"}}],
                    "messages": [{"id": "wamid.1", "from": "5214621349768",
                                  "type": "text", "text": {"body": "hola"}}],
                },
            }]}]
        }
        entrantes, echoes = extraer(payload)
        assert len(entrantes) == 1 and not echoes
        assert entrantes[0]["_nombre"] == "Kevin"

    def test_un_echo_se_reconoce_y_trae_el_destinatario(self) -> None:
        payload = {
            "entry": [{"changes": [{
                "field": "smb_message_echoes",
                "value": {"message_echoes": [
                    {"id": "wamid.2", "to": "5214621349768", "type": "text",
                     "text": {"body": "yo te contesto"}}
                ]},
            }]}]
        }
        entrantes, echoes = extraer(payload)
        assert not entrantes and len(echoes) == 1
        assert echoes[0]["para"] == "5214621349768"

    def test_un_payload_raro_no_revienta(self) -> None:
        """Un webhook que devuelve 500 se reintenta, y Meta acaba en bucle."""
        for basura in ({}, {"entry": None}, {"entry": [{"changes": [{}]}]}):
            assert extraer(basura) == ([], [])


class TestFirma:
    def test_sin_secreto_configurado_se_acepta(self) -> None:
        assert firma_valida(b"{}", None, "") is True

    def test_con_secreto_una_firma_falsa_se_rechaza(self) -> None:
        assert firma_valida(b"{}", "sha256=nope", "s3cr3t") is False

    def test_con_secreto_la_firma_correcta_pasa(self) -> None:
        import hashlib
        import hmac

        cuerpo = b'{"hola":1}'
        firma = hmac.new(b"s3cr3t", cuerpo, hashlib.sha256).hexdigest()
        assert firma_valida(cuerpo, f"sha256={firma}", "s3cr3t") is True


class TestTexto:
    def test_saca_el_texto_de_un_mensaje(self) -> None:
        assert texto_de({"type": "text", "text": {"body": " hola "}}) == "hola"

    def test_lo_que_no_es_texto_devuelve_none(self) -> None:
        """Para que quien llama pueda contestar con honestidad en vez de
        callarse: el silencio parece que el negocio no atiende."""
        assert texto_de({"type": "image", "image": {"id": "x"}}) is None
        assert texto_de({"type": "audio"}) is None


class TestConfig:
    def test_avisa_de_lo_que_falta(self) -> None:
        faltan = Settings(_env_file=None).faltantes()
        assert "WA_TOKEN" in faltan and "DATABASE_URL" in faltan

    def test_sin_parametros_de_muestreo_no_se_manda_nada(self) -> None:
        assert Settings(_env_file=None).muestreo == {}

    def test_la_allowlist_se_canonicaliza(self) -> None:
        s = Settings(_env_file=None, numeros_permitidos="5214621349768, 524629999999")
        assert s.identidades_permitidas == {"524621349768", "524629999999"}


class TestNumeroPropio:
    """Sin firma de Meta —imposible con una app de Tech Provider, cuyo secreto
    es uno solo para todos los clientes— esta es la comprobación que impide
    que un payload ajeno se conteste con la voz de este negocio."""

    def _payload(self, phone_number_id: str) -> dict:
        return {"entry": [{"changes": [{
            "field": "messages",
            "value": {
                "metadata": {"phone_number_id": phone_number_id},
                "messages": [{"id": "wamid.9", "from": "5214621349768",
                              "type": "text", "text": {"body": "hola"}}],
            },
        }]}]}

    def test_lo_dirigido_a_nuestro_numero_pasa(self) -> None:
        entrantes, _ = extraer(self._payload("1210878062111901"), "1210878062111901")
        assert len(entrantes) == 1

    def test_lo_dirigido_a_otro_numero_se_descarta(self) -> None:
        entrantes, echoes = extraer(self._payload("999999999999"), "1210878062111901")
        assert entrantes == [] and echoes == []

    def test_sin_numero_configurado_no_se_filtra(self) -> None:
        entrantes, _ = extraer(self._payload("999999999999"), None)
        assert len(entrantes) == 1
