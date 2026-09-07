# Bot informativo de WhatsApp

Microservicio FastAPI que contesta preguntas frecuentes en el WhatsApp de un
negocio. **Headless**: no hay panel, ni bandeja, ni CRM detrás. Solo el bot,
su base de datos y el conocimiento del negocio en un markdown.

Hecho para el **Colegio de Psicólogos de Irapuato, A.C.**, pero el conocimiento
está afuera del código: para otro negocio se cambia un archivo.

## Qué hace

- Contesta preguntas frecuentes con lo que dice `conocimiento/`. **Lo que no
  está ahí, no lo afirma**: lo dice y da el contacto del negocio.
- **Se calla cuando contesta una persona.** Si alguien responde desde la app de
  WhatsApp del teléfono, Meta manda un *echo* y el bot se aparta de esa
  conversación. Nada peor que la IA hablando encima de quien ya está atendiendo.
- **Vuelve solo.** Cuando el hilo lleva 24 h sin un solo mensaje, la IA se
  reactiva sin que nadie tenga que acordarse.
- Junta las ráfagas: "hola" + "oye" + "una pregunta" en cinco segundos es UN
  turno, no tres respuestas.
- Ignora los mensajes repetidos de Meta (reintenta cuando no recibe 200 a
  tiempo) para no contestar dos veces lo mismo.

## Lo que NO hace, a propósito

No agenda citas, no cobra, no guarda fichas de clientes, no tiene pipeline y no
expone ninguna pantalla. Si eso hace falta, el proyecto equivocado: eso es un
CRM.

## Arquitectura

```
WhatsApp del negocio
      │ webhook de Meta  →  POST /webhook/<segmento secreto>
      ▼
  este bot (FastAPI, puerto 8000)  ──► proveedor del modelo (OpenRouter)
      │                                 con el conocimiento del negocio
      ├──► Postgres: conversaciones, mensajes, estado de la pausa
      └──► Graph API de Meta: la respuesta sale directo
```

El token de WhatsApp vive aquí: no hay otro servicio al que delegarlo. Por eso
el webhook va detrás de un segmento secreto en la URL **y** de la firma de
Meta (`META_APP_SECRET`).

## Configurar

Copia `.env.example` a `.env` y llénalo. Lo obligatorio se valida al arrancar:
si falta algo, el contenedor no levanta en vez de quedarse mudo recibiendo
mensajes que no puede contestar.

Genera el secreto del webhook con:

```bash
openssl rand -hex 32
```

La URL que se registra en Meta queda:

```
https://TU-DOMINIO/webhook/<WEBHOOK_TOKEN>
```

Y el *verify token* del handshake es ese mismo valor.

## Desplegar en Coolify

1. App tipo **repositorio público**, build pack `dockerfile`, puerto `8000`.
2. **Healthcheck de Coolify APAGADO**: el Dockerfile ya trae el suyo, y el de
   Coolify mete la app en bucle de reinicio y todo responde 502.
3. **Sin Pre-Deployment Command**: las migraciones corren solas al arrancar.
4. Las variables de entorno solo se inyectan en un *deploy*. Si cambias una,
   **redespliega** — reiniciar no basta.

## Cambiar lo que sabe el bot

Editas `conocimiento/<negocio>.md` y redespliegas. No hay que tocar código.

Dos cosas que conviene tener presentes al escribirlo:

- **Cada carácter se paga en cada mensaje.** El conocimiento entero viaja al
  modelo en cada llamada; que sea preciso, no largo.
- Lo que **no** esté ahí, el bot lo va a negar. Es el comportamiento correcto,
  pero significa que un dato que falta se nota enseguida.

## Modo prueba

`NUMEROS_PERMITIDOS` con tu número: el bot solo te responde a ti. Los mensajes
de cualquier otra persona llegan y se guardan, pero sin respuesta automática.
Vaciar esa variable —y abrirlo a todo el mundo— es una decisión del dueño.

## Pruebas

```bash
pip install -r requirements-dev.txt
pytest -q
```

Cubren la lógica propia del bot: la pausa por intervención humana, la
reactivación por silencio, la canonicalización de números mexicanos, el parseo
del webhook y la verificación de firma.
