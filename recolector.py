"""
recolector.py — Etapa 1 del agregador de noticias global.

Lee la lista de medios de fuentes.csv, descarga sus feeds RSS y guarda los
artículos nuevos en noticias.db (SQLite), sin duplicados.

Uso:
    pip install feedparser
    python recolector.py              -> recolecta todas las fuentes activas
    python recolector.py --ver 20     -> muestra los últimos 20 titulares guardados
    python recolector.py --estado     -> muestra qué fuentes funcionan y cuáles fallan

En fuentes.csv, url_rss puede ser la dirección del feed RSS o, directamente,
la página principal del medio (ej: https://www.nytimes.com/): el script busca
solo el RSS dentro de la página y te avisa cuál encontró.

Opcionales:
    --fuentes ruta.csv   (por defecto: fuentes.csv junto al script)
    --db ruta.db         (por defecto: noticias.db junto al script)
"""

import argparse
import csv
import hashlib
import html
import re
import socket
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

try:
    import feedparser
except ImportError:
    sys.exit("Falta la librería feedparser. Instalala con:  pip install feedparser")

BASE = Path(__file__).resolve().parent
TIMEOUT_SEG = 20          # tiempo máximo de espera por medio
HILOS = 8                 # cuántos medios se consultan en paralelo
LARGO_RESUMEN = 300       # caracteres del resumen que se guardan
USER_AGENT = "Mozilla/5.0 (compatible; AgregadorNoticias/0.1)"

socket.setdefaulttimeout(TIMEOUT_SEG)

ESQUEMA = """
CREATE TABLE IF NOT EXISTS fuentes (
    id                   INTEGER PRIMARY KEY,
    nombre               TEXT UNIQUE NOT NULL,
    url_rss              TEXT NOT NULL,
    pais                 TEXT,          -- código ISO de 2 letras (AR, ES, JP...) o INT
    region               TEXT,
    idioma               TEXT,
    categoria            TEXT DEFAULT 'general',   -- general / deportes / ...
    activa               INTEGER DEFAULT 1,
    ultimo_intento       TEXT,
    ultimo_estado        TEXT,          -- OK / ERROR / VACIO
    ultimo_error         TEXT,
    articulos_ultima_vez INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS articulos (
    id               INTEGER PRIMARY KEY,
    url_hash         TEXT UNIQUE NOT NULL,   -- evita guardar dos veces el mismo artículo
    url              TEXT NOT NULL,
    titulo           TEXT NOT NULL,
    resumen          TEXT,
    imagen           TEXT,
    publicado_utc    TEXT,
    recolectado_utc  TEXT NOT NULL,
    fuente_id        INTEGER NOT NULL REFERENCES fuentes(id)
);

CREATE INDEX IF NOT EXISTS idx_articulos_publicado ON articulos(publicado_utc DESC);
CREATE INDEX IF NOT EXISTS idx_articulos_fuente    ON articulos(fuente_id);
"""


# ---------------------------------------------------------------- utilidades

def ahora_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalizar_url(url: str) -> str:
    """Quita parámetros de seguimiento (utm_*, fbclid...) y el #fragmento,
    para que el mismo artículo no se guarde dos veces con links distintos."""
    partes = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(partes.query)
             if not k.lower().startswith(("utm_", "fbclid", "gclid", "ref"))]
    return urlunsplit((partes.scheme, partes.netloc.lower(), partes.path, urlencode(query), ""))


def limpiar_texto(texto: str, largo: int | None = None) -> str:
    texto = re.sub(r"<[^>]+>", " ", texto or "")
    texto = html.unescape(texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    if largo and len(texto) > largo:
        texto = texto[:largo].rsplit(" ", 1)[0] + "…"
    return texto


def extraer_fecha(entrada) -> str | None:
    t = entrada.get("published_parsed") or entrada.get("updated_parsed")
    if not t:
        return None
    try:
        return datetime(*t[:6], tzinfo=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


ES_IMAGEN = re.compile(r"\.(jpe?g|png|webp|gif)(\?|$)", re.I)


def extraer_imagen(entrada) -> str | None:
    """Busca la foto del artículo en los lugares donde suelen ponerla los RSS."""
    for m in entrada.get("media_content", []) or []:
        url = m.get("url")
        if url and (m.get("medium") == "image" or "image" in (m.get("type") or "")
                    or ES_IMAGEN.search(url)):
            return url
    for m in entrada.get("media_thumbnail", []) or []:
        if m.get("url"):
            return m["url"]
    for enlace in entrada.get("links", []) or []:
        if enlace.get("rel") == "enclosure" and (enlace.get("type") or "").startswith("image"):
            return enlace.get("href")
    # Último recurso: una etiqueta <img> dentro del resumen o del contenido
    html_crudo = entrada.get("summary", "") or ""
    for c in entrada.get("content", []) or []:
        html_crudo += c.get("value", "") or ""
    m = re.search(r"<img[^>]+src=[\"']([^\"']+)[\"']", html_crudo, re.I)
    return m.group(1) if m else None


# ---------------------------------------------------------------- base de datos

def abrir_db(ruta: Path) -> sqlite3.Connection:
    con = sqlite3.connect(ruta)
    con.row_factory = sqlite3.Row
    con.executescript(ESQUEMA)
    # Bases creadas con la versión anterior no tienen la columna 'categoria'
    columnas = {c[1] for c in con.execute("PRAGMA table_info(fuentes)")}
    if "categoria" not in columnas:
        con.execute("ALTER TABLE fuentes ADD COLUMN categoria TEXT DEFAULT 'general'")
    return con


def cargar_fuentes(con: sqlite3.Connection, ruta_csv: Path) -> list[sqlite3.Row]:
    """Sincroniza fuentes.csv con la tabla 'fuentes' y devuelve las activas.
    Los medios que se borran del CSV quedan desactivados en la base
    (sus artículos ya guardados se conservan)."""
    nombres_csv = []
    with open(ruta_csv, encoding="utf-8-sig", newline="") as f:
        for fila in csv.DictReader(f):
            if not (fila.get("nombre") and fila.get("url_rss")):
                continue
            nombres_csv.append(fila["nombre"].strip())
            con.execute("""
                INSERT INTO fuentes (nombre, url_rss, pais, region, idioma, categoria, activa)
                VALUES (:nombre, :url_rss, :pais, :region, :idioma, :categoria, :activa)
                ON CONFLICT(nombre) DO UPDATE SET
                    url_rss=excluded.url_rss, pais=excluded.pais, region=excluded.region,
                    idioma=excluded.idioma, categoria=excluded.categoria, activa=excluded.activa
            """, {
                "nombre": fila["nombre"].strip(),
                "url_rss": fila["url_rss"].strip(),
                "pais": (fila.get("pais") or "").strip().upper(),
                "region": (fila.get("region") or "").strip(),
                "idioma": (fila.get("idioma") or "").strip().lower(),
                "categoria": (fila.get("categoria") or "general").strip().lower(),
                "activa": int((fila.get("activa") or "1").strip() or 1),
            })
    marcas = ",".join("?" * len(nombres_csv)) or "''"
    con.execute(f"UPDATE fuentes SET activa = 0 WHERE nombre NOT IN ({marcas})", nombres_csv)
    con.commit()
    return con.execute("SELECT * FROM fuentes WHERE activa = 1 ORDER BY region, nombre").fetchall()


# ---------------------------------------------------------------- recolección

LINK_RSS = re.compile(
    r"<link[^>]+type=[\"']application/(?:rss|atom)\+xml[\"'][^>]*>", re.I)


def descubrir_rss(url_pagina: str) -> str | None:
    """Si la URL es la página principal de un medio, busca el feed que anuncia
    en su HTML (<link rel="alternate" type="application/rss+xml" href=...>)."""
    try:
        req = Request(url_pagina, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=TIMEOUT_SEG) as r:
            pagina = r.read(500_000).decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return None
    for etiqueta in LINK_RSS.findall(pagina):
        m = re.search(r"href=[\"']([^\"']+)[\"']", etiqueta, re.I)
        if m:
            return urljoin(url_pagina, html.unescape(m.group(1)))
    return None


def explicar_error(feed, exc: Exception | None = None) -> str:
    """Traduce los errores técnicos más comunes a algo entendible."""
    estado_http = feed.get("status") if feed is not None else None
    if estado_http == 403:
        return "HTTP 403 — el sitio bloquea lectores automáticos"
    if estado_http == 404:
        return "HTTP 404 — esa dirección RSS ya no existe"
    if estado_http and estado_http >= 400:
        return f"HTTP {estado_http} — el sitio respondió con error"
    exc = exc or (feed.get("bozo_exception") if feed is not None else None)
    texto = f"{type(exc).__name__}: {exc}" if exc else "error desconocido"
    tipo = ((feed.get("headers") or {}).get("content-type", "") if feed is not None else "").lower()
    if "html" in tipo or "SAXParseException" in texto:
        return "La dirección es una página web, no un RSS (y no se encontró uno dentro)"
    if "getaddrinfo" in texto:
        return "No se encontró el sitio (dirección inexistente o sin internet)"
    if "CERTIFICATE_VERIFY_FAILED" in texto:
        return "El certificado de seguridad del sitio está mal configurado"
    if "timed out" in texto.lower():
        return "El sitio tardó demasiado en responder"
    return texto


def leer_feed(url: str):
    feed = feedparser.parse(url, agent=USER_AGENT)
    estado_http = feed.get("status")
    if (estado_http and estado_http >= 400) or (feed.bozo and not feed.entries):
        return None, explicar_error(feed)
    return feed, None


def descargar(fuente: sqlite3.Row) -> tuple[sqlite3.Row, object, str | None, str | None]:
    """Descarga un feed. Corre en paralelo (no toca la base de datos).
    Devuelve (fuente, feed, error, url_rss_descubierta)."""
    try:
        feed, error = leer_feed(fuente["url_rss"])
        if feed is not None and feed.entries:
            return fuente, feed, None, None
        # No era un feed (o vino vacío): buscar el RSS en esa página y en la portada del sitio
        partes = urlsplit(fuente["url_rss"])
        portada = f"{partes.scheme}://{partes.netloc}/"
        for pagina in dict.fromkeys([fuente["url_rss"], portada]):
            url_rss = descubrir_rss(pagina)
            if url_rss and url_rss != fuente["url_rss"]:
                feed2, error2 = leer_feed(url_rss)
                if feed2 is not None and feed2.entries:
                    return fuente, feed2, None, url_rss
                error = error or error2
        if feed is not None:
            return fuente, feed, None, None   # feed válido pero vacío
        return fuente, None, error, None
    except Exception as e:  # noqa: BLE001
        return fuente, None, f"{type(e).__name__}: {e}", None


def guardar(con: sqlite3.Connection, fuente: sqlite3.Row, feed) -> int:
    nuevos = 0
    recolectado = ahora_utc()
    for e in feed.entries:
        titulo = limpiar_texto(e.get("title", ""))
        url = e.get("link")
        if not titulo or not url:
            continue
        url_norm = normalizar_url(url)
        cur = con.execute("""
            INSERT OR IGNORE INTO articulos
                (url_hash, url, titulo, resumen, imagen, publicado_utc, recolectado_utc, fuente_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            hashlib.sha1(url_norm.encode()).hexdigest(),
            url,
            titulo,
            limpiar_texto(e.get("summary", ""), LARGO_RESUMEN),
            extraer_imagen(e),
            extraer_fecha(e) or recolectado,
            recolectado,
            fuente["id"],
        ))
        nuevos += cur.rowcount
    return nuevos


def recolectar(con: sqlite3.Connection, fuentes: list[sqlite3.Row]) -> None:
    print(f"Consultando {len(fuentes)} medios...\n")
    total_nuevos, fallidas, descubiertas = 0, [], []

    with ThreadPoolExecutor(max_workers=HILOS) as pool:
        tareas = [pool.submit(descargar, f) for f in fuentes]
        for tarea in as_completed(tareas):
            fuente, feed, error, url_descubierta = tarea.result()
            if url_descubierta:
                descubiertas.append((fuente["nombre"], url_descubierta))
            if error:
                estado, nuevos = "ERROR", 0
                fallidas.append((fuente["nombre"], error))
            else:
                nuevos = guardar(con, fuente, feed)
                estado = "OK" if feed.entries else "VACIO"
                total_nuevos += nuevos
            con.execute("""
                UPDATE fuentes SET ultimo_intento=?, ultimo_estado=?, ultimo_error=?,
                                   articulos_ultima_vez=? WHERE id=?
            """, (ahora_utc(), estado, error, nuevos, fuente["id"]))
            con.commit()
            marca = "✔" if estado == "OK" else ("✖" if estado == "ERROR" else "·")
            print(f"  {marca} {fuente['nombre']:<28} {fuente['region']:<14} nuevos: {nuevos}")

    print(f"\nListo. Artículos nuevos guardados: {total_nuevos}")
    if descubiertas:
        print("\nRSS encontrado automáticamente (podés pegarlo en fuentes.csv para ir más rápido):")
        for nombre, url in descubiertas:
            print(f"  - {nombre}: {url}")
    if fallidas:
        print(f"\nFuentes con problemas ({len(fallidas)}):")
        for nombre, err in fallidas:
            print(f"  - {nombre}: {err[:120]}")


# ---------------------------------------------------------------- consultas

def ver_ultimas(con: sqlite3.Connection, cantidad: int) -> None:
    filas = con.execute("""
        SELECT a.publicado_utc, f.nombre, f.pais, f.categoria, a.titulo, a.imagen IS NOT NULL AS foto
        FROM articulos a JOIN fuentes f ON f.id = a.fuente_id
        ORDER BY a.publicado_utc DESC LIMIT ?
    """, (cantidad,)).fetchall()
    for r in filas:
        foto = "📷" if r["foto"] else "  "
        etiqueta = "" if r["categoria"] == "general" else f"({r['categoria']}) "
        print(f"{r['publicado_utc'][:16].replace('T', ' ')}  {foto} [{r['pais']}] {etiqueta}"
              f"{r['nombre']}: {r['titulo']}")
    total = con.execute("SELECT COUNT(*) FROM articulos").fetchone()[0]
    print(f"\nTotal en la base: {total} artículos")


def ver_estado(con: sqlite3.Connection) -> None:
    filas = con.execute("""
        SELECT f.nombre, f.region, f.ultimo_estado, f.ultimo_error, COUNT(a.id) AS total
        FROM fuentes f LEFT JOIN articulos a ON a.fuente_id = f.id
        GROUP BY f.id ORDER BY f.ultimo_estado, f.region, f.nombre
    """).fetchall()
    for r in filas:
        print(f"{(r['ultimo_estado'] or '-'):<6} {r['nombre']:<28} {r['region'] or '':<14} "
              f"total: {r['total']:<5} {(r['ultimo_error'] or '')[:60]}")


# ---------------------------------------------------------------- main

def main() -> None:
    p = argparse.ArgumentParser(description="Recolector de noticias por RSS")
    p.add_argument("--fuentes", type=Path, default=BASE / "fuentes.csv")
    p.add_argument("--db", type=Path, default=BASE / "noticias.db")
    p.add_argument("--ver", type=int, metavar="N", help="mostrar los últimos N titulares")
    p.add_argument("--estado", action="store_true", help="mostrar el estado de cada fuente")
    args = p.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # para que Windows muestre bien acentos y emojis

    con = abrir_db(args.db)
    if args.ver:
        ver_ultimas(con, args.ver)
    elif args.estado:
        ver_estado(con)
    else:
        recolectar(con, cargar_fuentes(con, args.fuentes))
    con.close()


if __name__ == "__main__":
    main()
