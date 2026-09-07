"""App FastAPI: el webhook de Meta y poco más.

Headless a propósito: no hay panel ni bandeja. Lo único expuesto es el
webhook (bajo un segmento secreto) y `/health`.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.cerebro import Cerebro, cargar_conocimiento
from app.config import Settings
from app.db import Almacen
from app.meta import ClienteWhatsApp, extraer, firma_valida
from app.turno import Contexto, barrer_pausas, procesar_echo, procesar_entrante

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("bot.main")

RAIZ = Path(__file__).resolve().parent.parent
MIGRACIONES = RAIZ / "migraciones"

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    ctx: Contexto | None = getattr(request.app.state, "ctx", None)
    db_ok = False
    if ctx is not None:
        try:
            await ctx.almacen.pool.fetchval("SELECT 1")
            db_ok = True
        except Exception:
            db_ok = False
    return JSONResponse({"status": "ok", "db": "ok" if db_ok else "sin conexión"})


@router.get("/webhook/{token}")
async def verificar(token: str, request: Request) -> Response:
    """Handshake de Meta. Devuelve el challenge tal cual, en texto plano."""
    ctx: Contexto = request.app.state.ctx
    params = request.query_params
    if (
        token != ctx.settings.webhook_token
        or params.get("hub.mode") != "subscribe"
        or params.get("hub.verify_token") != ctx.settings.webhook_token
    ):
        logger.warning("handshake rechazado desde %s", request.client)
        return PlainTextResponse("forbidden", status_code=403)
    return PlainTextResponse(params.get("hub.challenge") or "")


@router.post("/webhook/{token}")
async def recibir(token: str, request: Request) -> Response:
    """Entrada de mensajes.

    Devuelve 200 casi siempre y a propósito: Meta reintenta lo que no se
    confirma rápido, y un 500 por un mensaje raro se convierte en un bucle de
    reintentos. El trabajo se hace después de contestar.
    """
    ctx: Contexto = request.app.state.ctx
    cuerpo = await request.body()

    # Un webhook de WhatsApp no pesa esto ni con el adjunto más generoso. El
    # tope evita que alguien que conozca la URL gaste memoria del contenedor
    # mandando cuerpos enormes.
    if len(cuerpo) > 512_000:
        logger.warning("payload de %d bytes: descartado por tamaño", len(cuerpo))
        return PlainTextResponse("payload too large", status_code=413)

    if token != ctx.settings.webhook_token:
        return PlainTextResponse("forbidden", status_code=403)
    if not firma_valida(
        cuerpo, request.headers.get("X-Hub-Signature-256"), ctx.settings.meta_app_secret
    ):
        logger.warning("firma inválida: payload descartado")
        return PlainTextResponse("forbidden", status_code=403)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": True})

    entrantes, echoes = extraer(payload, ctx.settings.wa_phone_number_id)
    for echo in echoes:
        asyncio.create_task(_seguro(procesar_echo(ctx, echo), "echo"))
    for mensaje in entrantes:
        asyncio.create_task(_seguro(procesar_entrante(ctx, mensaje), "entrante"))
    return JSONResponse({"ok": True})


async def _seguro(corutina, etiqueta: str) -> None:
    try:
        await corutina
    except Exception:
        logger.exception("fallo procesando %s", etiqueta)


async def _barrido_periodico(ctx: Contexto) -> None:
    """Cada 15 min devuelve a la IA lo que ya cumplió su silencio."""
    while True:
        try:
            await asyncio.sleep(900)
            await barrer_pausas(ctx)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("barrido de pausas: fallo")


def create_app(ctx: Contexto | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        propio = app.state.ctx is None
        if propio:
            settings = Settings()
            faltan = settings.faltantes()
            if faltan:
                # Arrancar a medias es peor que no arrancar: el bot recibiría
                # mensajes y no podría contestarlos, y nadie se enteraría.
                raise RuntimeError(
                    "faltan variables obligatorias: " + ", ".join(faltan)
                )
            if not settings.meta_app_secret:
                logger.warning(
                    "META_APP_SECRET vacío: no se verifica la firma de Meta. "
                    "El webhook queda protegido solo por el segmento secreto "
                    "de la URL."
                )
            almacen = Almacen(settings.database_url)
            await almacen.conectar()
            await almacen.migrar(MIGRACIONES)
            app.state.ctx = Contexto(
                settings=settings,
                almacen=almacen,
                wa=ClienteWhatsApp(
                    settings.wa_phone_number_id,
                    settings.wa_token,
                    settings.wa_graph_version,
                ),
                cerebro=Cerebro(
                    settings.llm_api_key,
                    settings.llm_model,
                    base_url=settings.llm_base_url,
                    muestreo=settings.muestreo,
                ),
                conocimiento=cargar_conocimiento(RAIZ / settings.conocimiento_path),
            )
            permitidas = app.state.ctx.settings.identidades_permitidas
            logger.info(
                "bot arriba · modelo %s · %s",
                settings.llm_model,
                (f"MODO PRUEBA: solo responde a {len(permitidas)} número(s)"
                 if permitidas else "responde a todos"),
            )
        c: Contexto = app.state.ctx
        barrido = asyncio.create_task(_barrido_periodico(c))
        try:
            yield
        finally:
            barrido.cancel()
            for tarea in list(c.pendientes.values()):
                tarea.cancel()
            if propio:
                await c.wa.cerrar()
                await c.almacen.cerrar()

    app = FastAPI(title="Bot informativo de WhatsApp", lifespan=lifespan)
    app.state.ctx = ctx
    app.include_router(router)
    return app


app = create_app()
