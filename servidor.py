"""
servidor.py — para usar la app en la notebook (y en el celular, con el mismo WiFi).

Muestra la misma pantalla que la versión publicada en GitHub, pero leyendo
tu noticias.db local, y con el botón "Buscar nuevas" que corre el recolector.

Uso:
    py servidor.py
Después abrí en el navegador la dirección que aparece en pantalla.
Para apagarlo: Ctrl + C en la ventana de PowerShell.
"""

import json
import re
import socket
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import exportar
import recolector as rec

PUERTO = 8000
ARCHIVO_DB = rec.BASE / "noticias.db"
ARCHIVO_HTML = rec.BASE / "index.html"
candado_recoleccion = threading.Lock()


def recolectar() -> dict:
    """Corre recolector.py y devuelve cuántos artículos nuevos trajo."""
    if not candado_recoleccion.acquire(blocking=False):
        return {"ok": False, "mensaje": "Ya hay una búsqueda en curso"}
    try:
        r = subprocess.run([sys.executable, str(rec.BASE / "recolector.py")], cwd=rec.BASE,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=600)
        m = re.search(r"Artículos nuevos guardados: (\d+)", r.stdout)
        if r.returncode != 0 or not m:
            return {"ok": False, "mensaje": "El recolector falló. Corré 'py recolector.py' para ver el error."}
        return {"ok": True, "nuevos": int(m.group(1))}
    except subprocess.TimeoutExpired:
        return {"ok": False, "mensaje": "La búsqueda tardó más de 10 minutos y se canceló"}
    finally:
        candado_recoleccion.release()


class Manejador(BaseHTTPRequestHandler):
    def _responder(self, codigo: int, cuerpo: bytes, tipo: str) -> None:
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def _json(self, datos: dict, codigo: int = 200) -> None:
        self._responder(codigo, json.dumps(datos, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                        "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        ruta = urlsplit(self.path).path
        try:
            if ruta in ("/", "/index.html"):
                self._responder(200, ARCHIVO_HTML.read_bytes(), "text/html; charset=utf-8")
            elif ruta == "/noticias.json":
                con = rec.abrir_db(ARCHIVO_DB)
                try:
                    self._json(exportar.exportar(con, local=True))
                finally:
                    con.close()
            else:
                self._json({"error": "No existe"}, 404)
        except sqlite3.OperationalError as e:
            self._json({"error": f"No se pudo leer noticias.db ({e})"}, 500)
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 500)

    def do_POST(self) -> None:  # noqa: N802
        if urlsplit(self.path).path == "/api/recolectar":
            self._json(recolectar())
        else:
            self._json({"error": "No existe"}, 404)

    def log_message(self, formato, *args) -> None:  # no mostrar cada pedido
        pass


def ip_local() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))   # no envía nada, solo elige la interfaz de red
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not ARCHIVO_DB.exists():
        sys.exit("No encuentro noticias.db. Corré primero:  py recolector.py")
    servidor = ThreadingHTTPServer(("0.0.0.0", PUERTO), Manejador)
    print("Servidor de noticias funcionando.\n")
    print(f"  En esta computadora:  http://localhost:{PUERTO}")
    print(f"  En el celular (mismo WiFi):  http://{ip_local()}:{PUERTO}\n")
    print("Para apagarlo: Ctrl + C")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor apagado.")


if __name__ == "__main__":
    main()
