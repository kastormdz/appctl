#!/usr/bin/env python3
"""El stack express-postgres-sftp: Express + React detras de un nginx.

QUE VERIFICA
  - que el stack exista con sus archivos, y que el CLI lo arme con el nombre
    correcto (express-postgres-sftp) y lo conozca como runtime
  - que nginx sirva el build de React como estatico (fallback SPA para las
    rutas) y proxye /api a Express SIN comerse el prefijo: con
    `proxy_pass http://127.0.0.1:3000/` (barra final) nginx recorta /api y las
    rutas del cliente dejan de matchear
  - que un asset inexistente devuelva 404 y no el index.html (el error
    "Unexpected token '<'" no dice nada de la causa real)
  - que el arranque sea /app/start.sh (no un `node server.js` fijo): el stack
    no puede obligar a una convencion que el cliente no tiene
  - que el entrypoint resuelva backend/ + frontend/ (fuente o ya buildeado) y
    que el stub sea Express DE VERDAD, instalado en la imagen
  - que el healthcheck mire nginx y no un /healthz de la app
  - que el smoke tenga el check de la API y lo despache para este stack
  - que el init de roles sea byte-identico al de los otros stacks postgres
  - que no haya ningun rm -rf sobre datos del cliente (regla dura del repo)

QUE NO VERIFICA: que la imagen construya ni que Express arranque. Eso es E2E con
docker (`appctl <p> express psql sftp` real + build + login SFTP).

Uso: python3 tests/test_express_stack.py
"""
from __future__ import annotations

import importlib.machinery
import pathlib
import sys
import contextlib
import io

RAIZ = pathlib.Path(__file__).resolve().parent.parent
NOMBRE = "express-postgres-sftp"
FALLOS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")
    if not cond:
        FALLOS.append(msg)


def leer(rel: str) -> str:
    p = RAIZ / rel
    return p.read_text(encoding="utf-8") if p.exists() else ""


def cargar_appctl():
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            m = importlib.machinery.SourceFileLoader("appctl_t", str(RAIZ / "bin" / "appctl")).load_module()
        except SystemExit:
            m = sys.modules.get("appctl_t")
    return m


def main() -> int:
    print("El stack express-postgres-sftp")
    d = RAIZ / "stacks" / NOMBRE
    print("\n=== 1. el stack existe y esta completo ===")
    check(d.is_dir(), "existe el directorio del stack")
    for f in ("compose.tmpl.yaml", "image/Dockerfile", "image/entrypoint.sh",
              "image/nginx.conf", "image/supervisord.conf", "image/sshd_config",
              "image/fixperms.sh", "image/stub-server.js", "init/10-roles.sh"):
        check((d / f).is_file(), f"tiene {f}")

    print("\n=== 2. el CLI lo conoce ===")
    appctl = cargar_appctl()
    # Primero lo que puede faltar de forma limpia: si `express` no esta en
    # RUNTIMES, stack_name() revienta con un TypeError y el test muere en vez
    # de reportar QUE falta (que es lo util cuando el cableado se rompe).
    check("express" in appctl.RUNTIMES, "express esta en RUNTIMES")
    check("express" in appctl.DEFAULT_TAGS, "express tiene tag default")
    check(appctl.runtime_of(NOMBRE) == "express", "runtime_of = express")
    check(appctl.stack_name({"express", "psql", "sftp"}) == NOMBRE,
          "stack_name({express,psql,sftp}) = express-postgres-sftp")
    check(appctl.DEFAULT_TAGS.get("express") == "22-alpine",
          "el tag de express es 22-alpine (mismo node que nextjs)")
    check(NOMBRE in appctl.RUNTIME_IMAGES, "esta en RUNTIME_IMAGES")
    check(appctl.image_tag(NOMBRE, "22-alpine").startswith("appctl/node-express-sftp:"),
          "la imagen es appctl/node-express-sftp")

    print("\n=== 3. nginx: estaticos + /api sin comerse el prefijo ===")
    ng = leer(f"stacks/{NOMBRE}/image/nginx.conf")
    check("try_files $uri $uri/ /index.html" in ng,
          "fallback SPA: las rutas del router devuelven el index.html")
    check("location /api/" in ng, "hay location /api/")
    check("proxy_pass http://127.0.0.1:3000;" in ng,
          "proxy_pass SIN barra final (con barra recorta /api y las rutas no matchean)")
    check("proxy_pass http://127.0.0.1:3000/;" not in ng,
          "y no aparece la variante con barra final")
    check("try_files $uri =404" in ng,
          "un asset inexistente da 404, no el index.html")
    check("root /app/frontend-dist;" in ng,
          "el root es la ruta estable del build (dist/build/out caen todas ahi)")
    check("sendfile      on;" in ng and "server_tokens off;" in ng,
          "los defaults seguros del resto de los stacks siguen")

    print("\n=== 4. el arranque no impone una convencion ===")
    sup = leer(f"stacks/{NOMBRE}/image/supervisord.conf")
    check("command=/app/start.sh" in sup,
          "supervisord corre /app/start.sh (no un node server.js fijo)")
    check("user=app" in sup and 'HOME="/home/app"' in sup,
          "el programa corre como app y con HOME explicito")
    check("[program:cron]" in sup and "autostart=false" in sup,
          "cron existe y no arranca solo (es bandera del proyecto)")

    print("\n=== 5. el entrypoint: backend + frontend + stub ===")
    ep = leer(f"stacks/{NOMBRE}/image/entrypoint.sh")
    check('BACKEND="/app/backend"' in ep and 'FRONTEND="/app/frontend"' in ep,
          "resuelve backend/ y frontend/ del upload")
    check("npm ci --omit=dev" in ep and "npm run build" in ep,
          "instala el backend y buildea el frontend")
    check("for _d in dist build out" in ep,
          "acepta dist/, build/ u out/ como salida del bundler")
    check("stub-server.js" in ep or "/opt/appctl/stub/server.js" in ep,
          "sin backend propio usa el stub de Express de la imagen")
    check("app: ok" in ep, "el stub del frontend trae el marcador 'app: ok'")
    check("exec npm start" in ep and "exec node server.js" in ep,
          "el comando lo decide el cliente (script start, o server.js/index.js/app.js)")
    check("start.sh" in ep and "chmod 755 /app/start.sh" in ep,
          "escribe /app/start.sh ejecutable")
    # regla dura del repo: nunca rm -rf sobre datos del cliente
    malos = [l for l in ep.splitlines()
             if "rm -rf" in l and ("upload" in l or "frontend" in l or "backend" in l
                                   or "WEBROOT" in l or "APP_SRC" in l)]
    check(not malos, f"ningun rm -rf sobre datos del cliente ({len(malos)} encontrados)")
    check("mkdir -p" in ep, "crea con mkdir -p, nunca borrando")

    print("\n=== 6. la imagen ===")
    dk = leer(f"stacks/{NOMBRE}/image/Dockerfile")
    check("FROM node:22-alpine" in dk, "base node:22-alpine (Node 22)")
    check("npm install express@4" in dk,
          "Express se instala en el BUILD (un stub que dependa de la red no verifica nada)")
    check("COPY stub-server.js /opt/appctl/stub/server.js" in dk,
          "el stub server va en la imagen")
    check("grep -qE '^[1-4]'" in dk,
          "el healthcheck mira nginx (cualquier 1xx-4xx), no un /healthz de la app")
    check("curl -fsS http://127.0.0.1/healthz" not in dk,
          "y NO quedo el healthcheck atado al archivo de la app")
    stub = leer(f"stacks/{NOMBRE}/image/stub-server.js")
    check("express ok" in stub and "/api/healthz" in stub,
          "el stub contesta /api/healthz con su marca")
    check("DB_HOST" in stub and "net.connect" in stub,
          "y prueba la base de verdad (no solo que el proceso arranque)")

    print("\n=== 7. el compose ===")
    cp = leer(f"stacks/{NOMBRE}/compose.tmpl.yaml")
    check("__APP_IMAGE__" in cp, "usa el placeholder de la imagen")
    check("start_period: 120s" in cp,
          "start_period largo: el arranque puede instalar y buildear")
    check("image: __APP_IMAGE__" in cp and "container_name: __PROYECTO___app" in cp,
          "los placeholders del resto de los stacks")

    print("\n=== 8. el smoke tiene el check de la API ===")
    sys.path.insert(0, str(RAIZ / "lib"))
    import smoke
    check(hasattr(smoke, "app_serves_api"), "smoke.app_serves_api existe")
    src = leer("lib/smoke.py")
    check('if "express" in (cfg.get("stack") or ""):' in src,
          "all_checks despacha el check de la API para el stack express")
    check("502" in src and "express ok" in src,
          "el check distingue el stub, el codigo real y un runtime caido")

    print("\n=== 9. el init de roles no diverge ===")
    ini = leer(f"stacks/{NOMBRE}/init/10-roles.sh")
    for otro in ("nextjs-postgres-sftp", "php-postgres-sftp",
                 "nextjs-python-postgres-sftp", "tomcat-postgres-sftp"):
        igual = ini == leer(f"stacks/{otro}/init/10-roles.sh")
        check(igual, f"init/10-roles.sh identico al de {otro}")

    print("\n=== 10. el proxy se persiste para los stacks de node ===")
    cli = leer("bin/appctl")
    check('if runtime_of(stack) in ("nextjs", "express"):' in cli,
          "el create guarda HTTP_PROXY/HTTPS_PROXY en el .env de los stacks node")
    check("os.environ.get(_k)" in cli,
          "y solo si el admin los tiene en el entorno (no inventa un proxy)")
    check("${HTTP_PROXY:-}" in leer(f"stacks/{NOMBRE}/compose.tmpl.yaml"),
          "el compose del stack los toma del .env")

    print("\n=== 11. los READMEs lo documentan ===")
    for n in ("README.md", "README.en.md"):
        t = leer(n)
        check(NOMBRE in t, f"{n} nombra el stack")
        check("Hay **6**" in t or "There are **6**" in t, f"{n} dice 6 stacks")

    print()
    if FALLOS:
        print(f"{len(FALLOS)} fallo(s):")
        for f in FALLOS:
            print(f"  - {f}")
        return 1
    print("OK: el stack express esta completo y cableado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
