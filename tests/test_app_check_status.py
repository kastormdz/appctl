#!/usr/bin/env python3
"""Los checks de app: 4xx en la raiz NO es stack roto.

QUE VERIFICA
  - con codigo real del cliente (expect_stub=False), un 2xx/3xx/4xx en / es
    PASS: lo que el check prueba es que nginx y el runtime se hablan, no que
    exista un index en la raiz. La app puede vivir en un subdirectorio.
  - un 5xx NO es PASS (fatal de PHP o fpm sin socket), ni la falta de respuesta.
  - en un proyecto recien creado (expect_stub=True) se sigue exigiendo el stub:
    si contesta otra cosa, el render o el stack estan mal.
  - lo mismo para la API python de nextjs-python.

POR QUE IMPORTA
  Medido: coplacteos tiene su app en /dpgleyfederal y devuelve 403 en /. Con
  `curl -f` el check daba FAIL y el upgrade de un proyecto productivo se
  revertia solo por esto (no por un problema real). Igual con cualquier app
  cuyo entrypoint no este en la raiz.

Como corre: levanta un servidor HTTP de verdad en un puerto libre y llama a
las funciones del smoke contra el. Un grep no reemplaza una respuesta.

Uso: python3 tests/test_app_check_status.py
"""
from __future__ import annotations

import pathlib
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RAIZ = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "lib"))
import smoke  # noqa: E402

FALLOS = []
RESP: list[tuple[int, str]] = [(200, "app: ok")]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        status, body = RESP[0]
        datos = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def log_message(self, *a):  # silencio
        pass


def check(nombre, cond, detalle=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {nombre}" + (f"\n        {detalle}" if detalle else ""))
    if not cond:
        FALLOS.append(nombre)


def puerto_libre() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = {"db_kind": "postgres"}

    try:
        print("=== codigo real del cliente (expect_stub=False) ===")
        for status, etiqueta in ((200, "200"), (301, "301"), (403, "403"), (404, "404")):
            RESP[0] = (status, "lo que sea")
            r = smoke.app_serves("", port, "p", cfg, expect_stub=False)
            check(f"HTTP {etiqueta} en / -> PASS (el stack contesta)", r.ok,
                  f"{r.name} | {r.detail}")
        for status, etiqueta in ((500, "500"), (502, "502"), (503, "503")):
            RESP[0] = (status, "fatal error")
            r = smoke.app_serves("", port, "p", cfg, expect_stub=False)
            check(f"HTTP {etiqueta} en / -> FAIL (el runtime no contesta)", not r.ok,
                  f"{r.name} | {r.detail}")

        print("\n=== sin respuesta ===")
        muerto = puerto_libre()
        r = smoke.app_serves("", muerto, "p", cfg, expect_stub=False)
        check("puerto cerrado -> FAIL", not r.ok, f"{r.name} | {r.detail}")

        print("\n=== proyecto recien creado: el stub sigue siendo obligatorio ===")
        RESP[0] = (403, "Forbidden")
        r = smoke.app_serves("", port, "p", cfg, expect_stub=True)
        check("HTTP 403 con expect_stub=True -> FAIL", not r.ok, f"{r.name} | {r.detail}")
        RESP[0] = (200, "app: ok\nphp: 8.5.11\n")
        r = smoke.app_serves("", port, "p", cfg, expect_stub=True)
        check("el stub (app: ok) -> PASS y nombra el runtime", r.ok and "php" in r.name,
              f"{r.name} | {r.detail}")
        r = smoke.app_serves("", port, "p", cfg, expect_stub=False)
        check("el mismo stub con expect_stub=False -> PASS", r.ok, f"{r.name}")
        RESP[0] = (200, "chau")
        r = smoke.app_serves("", port, "p", cfg, expect_stub=True)
        check("200 sin el marcador con expect_stub=True -> FAIL", not r.ok,
              f"{r.name} | {r.detail}")

        print("\n=== la API python de nextjs-python ===")
        RESP[0] = (401, "unauthorized")
        r = smoke.app_serves_python("", port, {"db_kind": "postgres"}, expect_stub=False)
        check("API real con 401 -> PASS", r.ok, f"{r.name} | {r.detail}")
        RESP[0] = (500, "uvicorn roto")
        r = smoke.app_serves_python("", port, {"db_kind": "postgres"}, expect_stub=False)
        check("API con 500 -> FAIL", not r.ok, f"{r.name} | {r.detail}")
        RESP[0] = (200, "app: python ok\n")
        r = smoke.app_serves_python("", port, {"db_kind": "postgres"}, expect_stub=True)
        check("el stub de python -> PASS", r.ok, f"{r.name} | {r.detail}")
    finally:
        srv.shutdown()

    print()
    if FALLOS:
        print(f"{len(FALLOS)} fallo(s):")
        for f in FALLOS:
            print(f"  - {f}")
        return 1
    print("todo bien")
    return 0


if __name__ == "__main__":
    sys.exit(main())
