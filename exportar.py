"""
exportar.py — convierte la base de noticias (noticias.db) en un archivo
noticias.json que la pantalla puede leer, y viceversa.

Uso:
    python exportar.py                          -> crea sitio/noticias.json
    python exportar.py --salida otra/ruta.json  -> lo guarda en otro lugar
    python exportar.py --importar anterior.json -> carga en noticias.db las
                                                   noticias de un JSON publicado

En GitHub se usa así: se baja el noticias.json ya publicado, se importa,
se corre el recolector y se vuelve a exportar. La página publicada funciona
como "memoria" entre una corrida y la siguiente.
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import recolector as rec

PAIS_LOCAL = "AR"      # tu país: define qué es "Cerca" en la pantalla
DIAS = 3               # cuántos días de noticias se publican
MAXIMO = 4000          # tope de artículos en el archivo (para que cargue rápido en el celular)
LARGO_RESUMEN = 200    # caracteres de resumen que se publican


def exportar(con, local: bool = False) -> dict:
    """Arma el contenido de noticias.json a partir de la base de datos."""
    desde = (datetime.now(timezone.utc) - timedelta(days=DIAS)).isoformat()
    filas = con.execute("""
        SELECT a.titulo, a.resumen, a.url, a.imagen, a.publicado_utc, a.fuente_id
        FROM articulos a JOIN fuentes f ON f.id = a.fuente_id
        WHERE a.publicado_utc >= ?
        ORDER BY a.publicado_utc DESC, a.id DESC
        LIMIT ?
    """, (desde, MAXIMO)).fetchall()

    ids_usados = sorted({f["fuente_id"] for f in filas})
    fuentes = {}
    for f in con.execute("SELECT * FROM fuentes ORDER BY id"):
        if f["id"] in ids_usados:
            fuentes[f["id"]] = len(fuentes)    # índice compacto dentro del JSON
    lista_fuentes = [
        {k: f[k] for k in ("nombre", "pais", "region", "idioma", "categoria")}
        for f in con.execute("SELECT * FROM fuentes ORDER BY id") if f["id"] in fuentes
    ]

    articulos = []
    for f in filas:
        resumen = f["resumen"] or ""
        if len(resumen) > LARGO_RESUMEN:
            resumen = resumen[:LARGO_RESUMEN].rsplit(" ", 1)[0] + "…"
        art = {"t": f["titulo"], "u": f["url"], "p": f["publicado_utc"], "f": fuentes[f["fuente_id"]]}
        if resumen and resumen != f["titulo"]:
            art["r"] = resumen
        if f["imagen"]:
            art["i"] = f["imagen"]
        articulos.append(art)

    return {
        "generado": rec.ahora_utc(),
        "pais_local": PAIS_LOCAL,
        "local": local,          # True cuando lo sirve servidor.py en la notebook
        "fuentes": lista_fuentes,
        "articulos": articulos,
    }


def importar(con, datos: dict) -> int:
    """Carga en la base las noticias de un noticias.json ya publicado."""
    ids = {}
    for i, f in enumerate(datos.get("fuentes", [])):
        con.execute("""INSERT OR IGNORE INTO fuentes (nombre, url_rss, pais, region, idioma, categoria, activa)
                       VALUES (?, '', ?, ?, ?, ?, 0)""",
                    (f["nombre"], f.get("pais"), f.get("region"), f.get("idioma"), f.get("categoria", "general")))
        ids[i] = con.execute("SELECT id FROM fuentes WHERE nombre = ?", (f["nombre"],)).fetchone()[0]

    cargados = 0
    generado = datos.get("generado") or rec.ahora_utc()
    for a in datos.get("articulos", []):
        if a.get("f") not in ids:
            continue
        cur = con.execute("""
            INSERT OR IGNORE INTO articulos
                (url_hash, url, titulo, resumen, imagen, publicado_utc, recolectado_utc, fuente_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (hashlib.sha1(rec.normalizar_url(a["u"]).encode()).hexdigest(), a["u"], a["t"],
              a.get("r", ""), a.get("i"), a["p"], generado, ids[a["f"]]))
        cargados += cur.rowcount
    con.commit()
    return cargados


def main() -> None:
    p = argparse.ArgumentParser(description="Exporta/importa noticias en formato JSON")
    p.add_argument("--db", type=Path, default=rec.BASE / "noticias.db")
    p.add_argument("--salida", type=Path, default=rec.BASE / "sitio" / "noticias.json")
    p.add_argument("--importar", type=Path, metavar="ARCHIVO", help="JSON publicado a cargar en la base")
    args = p.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    con = rec.abrir_db(args.db)
    if args.importar:
        if not args.importar.exists() or args.importar.stat().st_size == 0:
            print("No hay noticias publicadas todavía: se empieza de cero.")
            return
        try:
            datos = json.loads(args.importar.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("El archivo publicado no es válido: se empieza de cero.")
            return
        print(f"Noticias recuperadas de la publicación anterior: {importar(con, datos)}")
    else:
        datos = exportar(con)
        args.salida.parent.mkdir(parents=True, exist_ok=True)
        args.salida.write_text(json.dumps(datos, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tam = args.salida.stat().st_size / 1024
        print(f"Exportadas {len(datos['articulos'])} noticias de {len(datos['fuentes'])} medios "
              f"a {args.salida} ({tam:,.0f} KB)")
    con.close()


if __name__ == "__main__":
    main()
