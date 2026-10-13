"""
traductor.py — traduce al castellano los titulares (y resúmenes) de los medios
que publican en otros idiomas.

Se ejecuta solo: exportar.py lo llama antes de publicar, y servidor.py
después de "Buscar nuevas". También se puede correr a mano:
    py traductor.py

Cada noticia se traduce una sola vez: la traducción queda guardada (en la base
y en noticias.json), así que las corridas siguientes solo traducen lo nuevo.

Usa el traductor gratuito de Google (el mismo que usa la página
translate.google.com). Sirve para uso personal; si la app se abre a mucha
gente, conviene pasar a un servicio con clave (DeepL o Google Cloud).
"""

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import recolector as rec

# ------------------------------------------------------------------ configuración
IDIOMA_DESTINO = "es"
IDIOMAS_A_TRADUCIR = {"it", "de", "fr", "pt", "en"}   # el inglés se muestra traducido con el botón "Traducir"
MAXIMO_POR_CORRIDA = 600       # noticias por corrida; lo que quede se traduce en la siguiente
DIAS = 3                       # solo se traducen noticias recientes
LARGO_RESUMEN = 200            # igual que en exportar.py
CARACTERES_POR_PEDIDO = 1500   # textos que se agrupan en cada consulta al traductor
PAUSA_SEG = 0.4                # espera entre consultas, para no saturar el servicio

URL_TRADUCTOR = "https://translate.googleapis.com/translate_a/single"


class TraductorNoDisponible(Exception):
    """El servicio no respondió o cortó el acceso: se reintenta en la próxima corrida."""


# ------------------------------------------------------------------ base de datos

def preparar_db(con) -> None:
    """Agrega las columnas de traducción si la base es de una versión anterior."""
    columnas = {c[1] for c in con.execute("PRAGMA table_info(articulos)")}
    for nombre in ("titulo_tr", "resumen_tr", "idioma_tr"):
        if nombre not in columnas:
            con.execute(f"ALTER TABLE articulos ADD COLUMN {nombre} TEXT")
    con.commit()


def recortar(texto: str) -> str:
    texto = texto or ""
    if len(texto) > LARGO_RESUMEN:
        texto = texto[:LARGO_RESUMEN].rsplit(" ", 1)[0] + "…"
    return texto


# ------------------------------------------------------------------ traducción

def _consultar(texto: str, origen: str) -> str:
    consulta = urlencode({"client": "gtx", "sl": origen, "tl": IDIOMA_DESTINO, "dt": "t", "q": texto})
    req = Request(f"{URL_TRADUCTOR}?{consulta}", headers={"User-Agent": rec.USER_AGENT})
    try:
        with urlopen(req, timeout=rec.TIMEOUT_SEG) as r:
            datos = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001  (sin conexión, límite de uso, etc.)
        raise TraductorNoDisponible(f"{type(e).__name__}: {e}") from e
    return "".join(seg[0] for seg in (datos[0] or []) if seg and seg[0])


def traducir_lista(textos: list[str], origen: str) -> list[str]:
    """Traduce varios textos agrupándolos en pocas consultas (uno por renglón)."""
    resultado: list[str] = []
    grupo: list[str] = []

    def enviar(grupo: list[str]) -> None:
        traducido = _consultar("\n".join(grupo), origen).split("\n")
        if len(traducido) != len(grupo):          # si el traductor juntó renglones, de a uno
            traducido = []
            for t in grupo:
                traducido.append(_consultar(t, origen).replace("\n", " "))
                time.sleep(PAUSA_SEG)
        resultado.extend(x.strip() for x in traducido)
        time.sleep(PAUSA_SEG)

    for texto in textos:
        texto = texto.replace("\n", " ").strip()
        if grupo and sum(len(t) + 1 for t in grupo) + len(texto) > CARACTERES_POR_PEDIDO:
            enviar(grupo)
            grupo = []
        grupo.append(texto)
    if grupo:
        enviar(grupo)
    return resultado


def traducir_pendientes(con, mostrar=print) -> int:
    """Traduce las noticias recientes que todavía no tienen traducción."""
    preparar_db(con)
    idiomas = sorted(IDIOMAS_A_TRADUCIR - {IDIOMA_DESTINO})
    if not idiomas:
        return 0
    desde = (datetime.now(timezone.utc) - timedelta(days=DIAS)).isoformat()
    marcas = ",".join("?" * len(idiomas))
    filas = con.execute(f"""
        SELECT a.id, a.titulo, a.resumen, f.idioma
        FROM articulos a JOIN fuentes f ON f.id = a.fuente_id
        WHERE f.idioma IN ({marcas}) AND a.titulo_tr IS NULL AND a.publicado_utc >= ?
        ORDER BY a.publicado_utc DESC
        LIMIT ?
    """, [*idiomas, desde, MAXIMO_POR_CORRIDA]).fetchall()
    if not filas:
        return 0

    pendientes_total = len(filas)
    traducidas = 0
    try:
        for idioma in idiomas:
            lote = [f for f in filas if f["idioma"] == idioma]
            # de a 40 noticias, guardando cada tanda (si algo falla, lo hecho no se pierde)
            for i in range(0, len(lote), 40):
                tanda = lote[i:i + 40]
                titulos = traducir_lista([f["titulo"] for f in tanda], idioma)
                resumenes_orig = [recortar(f["resumen"]) for f in tanda]
                con_resumen = [r for r in resumenes_orig if r]
                resumenes_tr = iter(traducir_lista(con_resumen, idioma)) if con_resumen else iter([])
                for f, titulo, resumen in zip(tanda, titulos, resumenes_orig):
                    con.execute("UPDATE articulos SET titulo_tr=?, resumen_tr=?, idioma_tr=? WHERE id=?",
                                (titulo or f["titulo"], next(resumenes_tr, "") if resumen else "",
                                 IDIOMA_DESTINO, f["id"]))
                con.commit()
                traducidas += len(tanda)
    except TraductorNoDisponible as e:
        mostrar(f"El traductor no respondió ({str(e)[:80]}). "
                f"Lo pendiente se traduce en la próxima actualización.")
    mostrar(f"Noticias traducidas: {traducidas} de {pendientes_total}")
    return traducidas


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    con = rec.abrir_db(rec.BASE / "noticias.db")
    traducir_pendientes(con)
    con.close()


if __name__ == "__main__":
    main()
