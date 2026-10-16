"""
analisis.py — dos cálculos que hace exportar.py antes de publicar:

1. Agrupar: detecta notas de distintos medios que hablan del mismo hecho
   (comparando las palabras importantes de los titulares) para mostrarlas
   juntas: "5 medios de 4 países".
2. Tendencias: los nombres propios (personas, lugares, equipos, organismos)
   que más medios distintos mencionaron en las últimas horas.

Todo se calcula solo con los titulares, igual para todos los usuarios:
no interviene nada de lo que cada persona lee o toca.

Para comparar medios en distintos idiomas se usa el título traducido al
castellano cuando existe (ver traductor.py).
"""

import math
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta

# ------------------------------------------------------------------ configuración
SIMILITUD_MINIMA = 0.33     # 0 a 1: qué tan parecidos tienen que ser dos titulares
VENTANA_HORAS = 36          # solo se agrupan notas publicadas con menos de esta diferencia
HORAS_TENDENCIAS = 12       # período que se mira para las tendencias
MEDIOS_MINIMOS_TENDENCIA = 3
CANTIDAD_TENDENCIAS = 8

PALABRAS_VACIAS = set("""
a al algo algun alguna algunas alguno algunos ante antes aqui asi aun aunque bajo bien cada casi
como con contra cual cuales cuando cuanto de del desde donde dos durante e el ella ellas ellos en
entre era eran es esa esas ese eso esos esta estaba estado estan estar estas este esto estos fue
fueron ha habia han hasta hay hoy la las le les lo los mas me mientras mismo mucho muy nada ni no
nos nuestro o otra otras otro otros para pero poco por porque que quien quienes se segun ser si
sido sin sobre son su sus tambien tan tanto te tiene tienen todo todos tras tu un una unas uno
unos ya y yo
dijo dice dicen asegura afirma anuncia anuncian revela explica advierte confirma pide piden
nuevo nueva nuevos nuevas ultimo ultima ultimos ultimas vivo video videos foto fotos minuto
minutos hora horas dia dias semana ano anos vez veces primer primera mil millones caso parte
puede pueden podria hacer hace hecho tener tras momento forma manera cosas cosa gran grandes
gobierno gobiernos presidente presidenta ministro ministra oficial
""".split())

PALABRAS_MAYUSCULA_COMUNES = set("""
El La Los Las Lo Un Una Unos Unas En Por Para Con Sin Sobre Tras Desde Hasta Entre Ante Bajo
Qué Cómo Cuándo Dónde Quién Quiénes Cuál Cuáles Por qué Este Esta Estos Estas Ese Esa Eso Así
Hoy Ayer Mañana Ahora Aquí Ya No Sí Y O Pero Más Menos Muy Todo Todos Toda Todas Nuevo Nueva
Lunes Martes Miércoles Jueves Viernes Sábado Domingo
Enero Febrero Marzo Abril Mayo Junio Julio Agosto Septiembre Setiembre Octubre Noviembre Diciembre
Gobierno Estado Video Fotos Foto Últimas Última Último Noticias Opinión Análisis Exclusivo
Mundo País Ciudad Presidente Ministro Ministra Señor Señora Dios
""".split())

PALABRA = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9]+(?:[.'’][A-Za-zÀ-ÖØ-öø-ÿ]+)*")


def sin_tildes(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def texto_castellano(art: dict, fuente: dict) -> str | None:
    """El titular en castellano: el original si el medio es en castellano, o la traducción."""
    if fuente.get("idioma") == "es":
        return art["t"]
    return art.get("tt")


NOMBRE_DOBLE = re.compile(r"\b([A-ZÀ-Ö][a-zà-ÿ]+) ([A-ZÀ-Ö][a-zà-ÿ]+)\b")


def nombres_dobles(textos: list[str]) -> set[str]:
    """Nombres de dos palabras que se repiten ("Buenos Aires", "Casa Blanca"):
    se tratan como una sola palabra, para que no cuenten como dos coincidencias."""
    conteo = Counter(sin_tildes(f"{a} {b}").lower() for t in textos for a, b in NOMBRE_DOBLE.findall(t))
    return {n for n, c in conteo.items() if c >= 2}


def raices(texto: str, dobles: set[str] = frozenset()) -> set[str]:
    """Palabras importantes del titular, simplificadas para comparar
    ("elecciones" y "elección" quedan iguales)."""
    salida = set()
    texto = sin_tildes(texto)
    for n in dobles:
        texto = re.sub(rf"\b{re.escape(n)}\b", n.replace(" ", ""),
                       texto, flags=re.IGNORECASE)
    for original in PALABRA.findall(texto):
        p = original.lower()
        sigla = original.isupper() and 2 <= len(original) <= 5     # FMI, ONU, NBA, OTAN
        if p.isdigit() or p in PALABRAS_VACIAS or (len(p) < 4 and not sigla):
            continue
        salida.add(p[:6])
    return salida


# ------------------------------------------------------------------ 1. agrupar

def agrupar(articulos: list[dict], fuentes: list[dict]) -> None:
    """Agrega a cada artículo agrupado la clave "g" (número de grupo).
    Solo quedan grupos con al menos dos medios distintos."""
    textos = [(i, texto_castellano(a, fuentes[a["f"]])) for i, a in enumerate(articulos)]
    dobles = nombres_dobles([t for _, t in textos if t])
    datos = []
    for i, texto in textos:
        if texto:
            r = raices(texto, dobles)
            if len(r) >= 3:
                datos.append((i, r, datetime.fromisoformat(articulos[i]["p"])))
    if not datos:
        return

    # peso de cada palabra: las raras (nombres, lugares) valen más que las comunes
    df = Counter(r for _, rs, _ in datos for r in rs)
    total = len(datos)
    idf = {r: math.log(total / n) for r, n in df.items()}

    def peso(rs):
        return sum(idf[r] for r in rs)

    comun = max(8, total * 0.1)       # palabras demasiado frecuentes no cuentan como coincidencia

    grupos = []                       # cada grupo: lista de (raíces, norma, fecha, índice)
    indice = defaultdict(set)         # raíz -> grupos que la contienen (para comparar rápido)
    datos.sort(key=lambda d: d[2], reverse=True)
    ventana = timedelta(hours=VENTANA_HORAS)

    for i, rs, fecha in datos:
        norma = peso(rs)
        candidatos = {g for r in rs if df[r] <= comun for g in indice[r]}
        mejor, mejor_sim = None, SIMILITUD_MINIMA
        for g in candidatos:
            for rs2, n2, f2, _ in grupos[g]:           # se compara con cada nota del grupo
                if abs(f2 - fecha) > ventana:
                    continue
                comunes = rs & rs2
                if sum(1 for r in comunes if df[r] <= comun) < 2:
                    continue
                sim = peso(comunes) / math.sqrt(norma * n2)
                if sim > mejor_sim:
                    mejor, mejor_sim = g, sim
        if mejor is None:
            grupos.append([])
            mejor = len(grupos) - 1
        grupos[mejor].append((rs, norma, fecha, i))
        for r in rs:
            indice[r].add(mejor)

    numero = 0
    for grupo in grupos:
        miembros = [i for *_, i in grupo]
        medios = {articulos[i]["f"] for i in miembros}
        if len(medios) >= 2:
            for i in miembros:
                articulos[i]["g"] = numero
            numero += 1


# ------------------------------------------------------------------ 2. tendencias

def _nombres_propios(texto: str) -> list[tuple[str, bool]]:
    """Secuencias de palabras con mayúscula (hasta 3, permitiendo "de"/"del" en el medio).
    Devuelve (frase, está_al_principio_del_titular)."""
    palabras = PALABRA.findall(texto)
    salida, actual, inicio = [], [], 0
    def cerrar():
        while actual and actual[-1] in ("de", "del"):
            actual.pop()
        if actual:
            salida.append((" ".join(actual), inicio == 0))
    for pos, p in enumerate(palabras):
        es_nombre = (p[0].isupper() and p not in PALABRAS_MAYUSCULA_COMUNES) or \
                    (p.isupper() and 2 <= len(p) <= 5)
        if es_nombre and len(actual) < 3:
            if not actual:
                inicio = pos
            actual.append(p)
        elif p in ("de", "del") and actual and len(actual) < 3:
            actual.append(p)
        else:
            cerrar()
            actual, inicio = ([p], pos) if es_nombre else ([], pos)
    cerrar()
    return salida


def tendencias(articulos: list[dict], fuentes: list[dict], generado: str) -> list[dict]:
    desde = datetime.fromisoformat(generado) - timedelta(hours=HORAS_TENDENCIAS)
    nombres_medios = {sin_tildes(f["nombre"]).lower() for f in fuentes}
    medios = defaultdict(set)
    paises = defaultdict(set)
    forma = defaultdict(Counter)           # cómo se escribe más seguido cada frase
    a_mitad = set()                        # frases vistas en el medio de un titular

    recientes = [a for a in articulos if datetime.fromisoformat(a["p"]) >= desde]
    frases_por_art = []
    for a in recientes:
        texto = texto_castellano(a, fuentes[a["f"]])
        if not texto:
            continue
        frases = _nombres_propios(texto)
        frases_por_art.append((a, frases))
        for frase, al_inicio in frases:
            if not al_inicio or " " in frase:
                a_mitad.add(sin_tildes(frase).lower())
            if " " in frase:                      # "Donald Trump" también cuenta como "Trump"
                a_mitad.update(sin_tildes(w).lower() for w in frase.split() if w not in ("de", "del"))

    notas = defaultdict(set)               # qué artículos mencionan cada frase
    for a, frases in frases_por_art:
        candidatas = []
        for frase, al_inicio in frases:
            candidatas.append((frase, al_inicio))
            if " " in frase:
                candidatas += [(w, False) for w in frase.split() if w not in ("de", "del")]
        for frase, al_inicio in candidatas:
            clave = sin_tildes(frase).lower()
            # una palabra suelta al principio del titular puede ser solo la mayúscula inicial
            if al_inicio and " " not in frase and clave not in a_mitad:
                continue
            if clave in nombres_medios or len(clave) < 3:
                continue
            notas[clave].add(id(a))
            medios[clave].add(a["f"])
            paises[clave].add(fuentes[a["f"]]["pais"])
            forma[clave][frase] += 1

    puntaje = {k: len(medios[k]) + 0.5 * len(paises[k]) for k in medios
               if len(medios[k]) >= MEDIOS_MINIMOS_TENDENCIA}
    elegidas = []
    for k in sorted(puntaje, key=puntaje.get, reverse=True):
        palabras = set(k.split())
        # evitar repetidos: "Trump" y "Donald Trump" -> queda la que más medios tiene
        if any(palabras & set(e.split()) for e in elegidas):
            continue
        # evitar varias tendencias de la misma historia ("Israel", "Hamás", "Gaza")
        if any(len(notas[k] & notas[e]) >= 0.6 * len(notas[k]) for e in elegidas):
            continue
        elegidas.append(k)
        if len(elegidas) == CANTIDAD_TENDENCIAS:
            break
    return [{"t": forma[k].most_common(1)[0][0], "n": len(medios[k]), "c": len(paises[k])}
            for k in elegidas]
