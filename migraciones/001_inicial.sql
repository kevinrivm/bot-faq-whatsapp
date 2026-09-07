-- Estado mínimo de un bot informativo: quién escribió, qué se dijo, y si la
-- IA está en pausa porque un humano tomó la conversación.

CREATE TABLE IF NOT EXISTS conversaciones (
    identidad          TEXT PRIMARY KEY,          -- wa_id canónico (52XXXXXXXXXX)
    nombre             TEXT,
    -- Cuándo se vio el ÚLTIMO mensaje del hilo, venga de quien venga. Es el
    -- reloj de la reactivación: 24 h de silencio y la IA vuelve sola.
    ultimo_mensaje_en  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NULL = la IA contesta. Con fecha = un humano tomó la conversación.
    pausada_en         TIMESTAMPTZ,
    -- Por qué se pausó: 'humano' (echo del teléfono) o 'manual'.
    pausada_por        TEXT,
    creada_en          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mensajes (
    -- El id de Meta es la clave: es lo que hace la ingesta idempotente. Meta
    -- reintenta el webhook cuando no recibe 200 a tiempo, y sin esto el bot
    -- contestaría dos veces al mismo mensaje.
    id            TEXT PRIMARY KEY,
    identidad     TEXT NOT NULL REFERENCES conversaciones(identidad) ON DELETE CASCADE,
    direccion     TEXT NOT NULL,                  -- entrante | saliente
    origen        TEXT NOT NULL DEFAULT 'lead',   -- lead | ia | humano
    texto         TEXT,
    creado_en     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS mensajes_por_hilo
    ON mensajes (identidad, creado_en DESC);

CREATE INDEX IF NOT EXISTS conversaciones_pausadas
    ON conversaciones (pausada_en) WHERE pausada_en IS NOT NULL;
