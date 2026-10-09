#!/usr/bin/env python3
"""El stack nextjs-python: dos runtimes, un puerto, un chroot.

QUE VERIFICA
  - que el stack exista y que el CLI lo arme con el nombre correcto
    (nextjs-python-postgres-sftp) y valide lo que no corresponde
  - que nginx rutee /api al API python y el resto a Next
  - que supervisord levante LOS DOS runtimes (una API caida no puede
    pasar por proyecto sano)
  - que el healthcheck cubra los dos
  - que el chroot no traiga andamiaje (bin/usr/lib/etc/proc/dev) y que el
    arranque no borre NUNCA el codigo del cliente
  - que el smoke y el resumen del developer contemplen la API

QUE NO VERIFICA
  - que la imagen construya ni que uvicorn levante. Eso se prueba con un
    `appctl <proyecto> nextjs python psql sftp` real (build + contenedor +
    login SFTP): es la regla del repo, un grep no reemplaza un arranque.

Uso: python3 tests/test_nextjs_python.py
"""
from __future__ import annotations

import importlib.util
import inspect
import re
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STACK = ROOT / "stacks" / "nextjs-python-postgres-sftp"
NOMBRE = "nextjs-python-postgres-sftp"


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  PASS  {msg}")


def leer(rel: str) -> str:
    p = ROOT / rel
    if not p.is_file():
        raise AssertionError(f"no existe {rel}")
    return p.read_text()


def cargar_appctl():
    """El bin/appctl real, importado como modulo.

    No es un .py (no tiene extension) asi que se carga con SourceFileLoader.
    Importarlo es lo que permite probar la FUNCION (stack_name,
    validate_components, image_tag) y no un texto que se parece.
    """
    sys.path.insert(0, str(ROOT / "lib"))
    loader = SourceFileLoader("appctl_bajo_test", str(ROOT / "bin" / "appctl"))
    spec = importlib.util.spec_from_loader("appctl_bajo_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def main() -> int:
    print("Stack nextjs-python (Next + API FastAPI)")
    appctl = cargar_appctl()

    # ---- 1. el CLI arma y valida el stack ----
    print("\n=== el CLI: nombre y validacion ===")
    check(appctl.stack_name({"nextjs", "python", "psql", "sftp"}) == NOMBRE,
          f"stack_name(nextjs+python+psql+sftp) = {NOMBRE}")
    check(appctl.stack_name({"nextjs", "psql", "sftp"}) ==
          "nextjs-postgres-sftp",
          "sin python el stack sigue siendo nextjs-postgres-sftp")
    check(appctl.runtime_of(NOMBRE) == "nextjs",
          "el runtime del stack combinado es nextjs (comparten imagen base)")
    check(NOMBRE in appctl.RUNTIME_IMAGES,
          "el stack esta en RUNTIME_IMAGES (sin esto el build no sabe la imagen)")
    check(appctl.image_tag(NOMBRE, "22-alpine").startswith("appctl/node-python-sftp:"),
          "la imagen del stack es appctl/node-python-sftp")

    for comps, motivo in (
        ({"python", "psql", "sftp"}, "python solo (sin nextjs)"),
        ({"nextjs", "python", "psql"}, "python sin sftp"),
    ):
        try:
            appctl.validate_components(comps)
            raise AssertionError(f"{motivo}: validate_components lo acepto")
        except SystemExit:
            pass
        print(f"  PASS  {motivo} se rechaza")
    appctl.validate_components({"nextjs", "python", "psql", "sftp", "cron"})
    print("  PASS  la combinacion valida pasa")

    # ---- 2. los archivos del stack ----
    print("\n=== estructura ===")
    for rel in ("compose.tmpl.yaml", "init/10-roles.sh",
                "image/Dockerfile", "image/entrypoint.sh", "image/nginx.conf",
                "image/sshd_config", "image/supervisord.conf",
                "image/python-launch.sh"):
        check((STACK / rel).is_file(), f"existe {rel}")

    # ---- 3. nginx: un puerto, dos runtimes, y la API INTERNA ----
    #
    # El default es el de la arquitectura BFF: /api lo contesta NEXT (sus
    # route handlers) y uvicorn vive interno en 127.0.0.1:8000. El bloque que
    # publica la API existe, pero ENTRE MARCADORES: el entrypoint lo borra si
    # PY_API_PUBLICA != 1. Si alguien lo saca de los marcadores, el default
    # vuelve a exponer la API al navegador sin que nadie lo note: por eso se
    # verifica la posicion, no solo la existencia.
    print("\n=== nginx: /api lo sirve Next (BFF), la API queda interna ===")
    ng = leer(f"stacks/{NOMBRE}/image/nginx.conf")
    check("location = /healthz" in ng and "proxy_pass http://127.0.0.1:3000" in ng,
          "/healthz lo contesta Next (el healthcheck depende de eso)")
    check(ng.count("proxy_pass http://127.0.0.1:3000") >= 2,
          "el resto del trafico va a Next")
    check("deny all;" in ng and "/private" in ng,
          "/private queda denegado en nginx (barrera del dir privado)")
    ini = ng.find("# __PY_API_PUBLICA_INICIO__")
    fin = ng.find("# __PY_API_PUBLICA_FIN__")
    check(ini != -1 and fin != -1 and ini < fin,
          "el bloque de la API publicada esta entre marcadores")
    bloque = ng[ini:fin] if ini != -1 and fin != -1 and ini < fin else ""
    check(re.search(r"location /api/ \{", bloque) is not None,
          "el location /api/ esta DENTRO de los marcadores")
    check("proxy_pass http://127.0.0.1:8000/" in bloque,
          "el /api/ publicado va a uvicorn (127.0.0.1:8000)")
    # el prefijo se saca: si no, el cliente tendria que escribir /api/ en
    # cada ruta de su API
    check(re.search(r"location /api/\s*\{\s*proxy_pass http://127\.0\.0\.1:8000/",
                    bloque, re.S) is not None,
          "el prefijo /api/ se SACA al reenviar (proxy_pass con / final)")
    check("location /api/" not in ng[:ini] + ng[fin:],
          "FUERA de los marcadores no queda ningun /api: el default es interno")
    check("route handlers = la capa BFF" in ng,
          "el conf explica que /api lo sirve el BFF de Next")

    # ---- 3b. el entrypoint recorta el bloque ----
    print("\n=== el entrypoint: PY_API_PUBLICA decide ===")
    ep = leer(f"stacks/{NOMBRE}/image/entrypoint.sh")
    check("PY_API_PUBLICA" in ep, "el entrypoint lee PY_API_PUBLICA")
    check("__PY_API_PUBLICA_INICIO__" in ep and "__PY_API_PUBLICA_FIN__" in ep,
          "recorta por los marcadores")
    check("-s /etc/nginx/nginx.conf.nuevo" in ep,
          "si el recorte sale vacio deja el conf original (nginx no arranca sin conf)")

    # ---- 4. supervisord: los dos runtimes ----
    print("\n=== supervisord ===")
    sup = leer(f"stacks/{NOMBRE}/image/supervisord.conf")
    check("[program:next]" in sup, "next corre bajo supervisord")
    check("[program:python]" in sup, "la API python corre bajo supervisord")
    check("command=/usr/local/bin/python-launch.sh" in sup,
          "el programa python usa el launcher (elige el target de uvicorn)")
    check("[program:sshd]" in sup and "[program:nginx]" in sup,
          "nginx y sshd siguen corriendo")
    check(re.search(r"\[program:python\]\n.*?user=app", sup, re.S) is not None,
          "uvicorn NO corre como root")

    # ---- 5. el healthcheck cubre los dos ----
    print("\n=== el healthcheck no puede mentir ===")
    tmpl = leer(f"stacks/{NOMBRE}/compose.tmpl.yaml")
    hc = re.search(r"test: \[\"CMD-SHELL\", \"(.*?)\"\]", tmpl).group(1)
    check("/healthz" in hc, "el healthcheck pide /healthz a Next")
    check("http://127.0.0.1:8000/healthz" in hc,
          "y la API DIRECTO a 127.0.0.1:8000 (no depende del ruteo de nginx)")
    check("/api/healthz" not in hc,
          "no la pide por /api: ese path ahora lo sirve el BFF")
    df = leer(f"stacks/{NOMBRE}/image/Dockerfile")
    check("http://127.0.0.1:8000/healthz" in df,
          "el HEALTHCHECK de la imagen tambien cubre la API (directo)")
    check('PY_API_PUBLICA: "${PY_API_PUBLICA:-0}"' in tmpl,
          "PY_API_PUBLICA va en el entorno (default 0: interna)")
    check('PY_API_URL: "http://127.0.0.1:8000"' in tmpl,
          "PY_API_URL va en el entorno: es la direccion que usa el BFF")
    # el check del CLI tiene que saber mirar adentro cuando no esta publicada
    smoke = leer("lib/smoke.py")
    check('_container_env_of(app, "PY_API_PUBLICA")' in smoke,
          "el check del CLI pregunta si la API esta publicada")
    check('"http://127.0.0.1:8000/"' in smoke,
          "y la pide adentro del contenedor cuando es interna")
    check('PY_PORT: "8000"' in tmpl, "PY_PORT va en el entorno del contenedor")

    # ---- 6. la API: venv, deps y stub ----
    print("\n=== API python: venv y deps ===")
    check("python3 py3-pip" in df, "python entra por apk (la imagen base tiene node)")
    check("/opt/venv" in df and "python3 -m venv /opt/venv" in df,
          "las deps van a un venv, no al python del sistema")
    check("externally-managed-environment" in df,
          "el Dockerfile explica POR QUE el venv (PEP 668)")
    check("fastapi" in df and "uvicorn[standard]" in df,
          "fastapi y uvicorn vienen preinstalados (el stub anda sin red)")
    launch = leer(f"stacks/{NOMBRE}/image/python-launch.sh")
    check("/opt/venv/bin/uvicorn" in launch, "uvicorn se arranca con el venv")
    check("main:app" in launch and "app.main:app" in launch,
          "el launcher acepta main:app y app.main:app")
    check('PY_PORT:-8000' in launch, "el puerto sale de PY_PORT con default 8000")
    check("--host 127.0.0.1" in launch,
          "uvicorn escucha solo en loopback (sale por nginx, no al host)")

    # ---- 7. el entrypoint: deps, stub, y nada de rm -rf ----
    print("\n=== entrypoint ===")
    ep = leer(f"stacks/{NOMBRE}/image/entrypoint.sh")
    check("app: python ok" in ep, "el stub de la API tiene su marca verificable")
    check("app: ok" in ep, "el stub de Next sigue con la suya")
    check("pip\" install --no-cache-dir" in ep or "pip install" in ep,
          "el entrypoint instala el requirements.txt del cliente")
    check("requirements.txt" in ep, "requiere backend/requirements.txt")
    # el rc tiene que mirarse de verdad: `pip ... | tail -8 || { }` deja que
    # el exit code lo defina tail (0) y un pip fallido pasa por bueno.
    # Se miran las LINEAS DE CODIGO, no los comentarios: el comentario que
    # explica el anti-patron lo nombra, y un grep ingenuo lo toma por bug.
    ep_codigo = "\n".join(l for l in ep.splitlines()
                          if not l.strip().startswith("#"))
    check(not re.search(r"pip.{0,40}\|\s*tail", ep_codigo),
          "el pip install NO esconde el fallo detras de un pipe a tail")
    check(not re.search(r"pnpm install.{0,40}\|\s*tail", ep_codigo),
          "el pnpm install tampoco: el rc se mira")
    check('rm -rf "${SFTP_CHROOT}/upload"' not in ep,
          "el arranque NO borra /upload (el rm -rf borraba el codigo real)")
    check('rm -rf "${SFTP_CHROOT:?}/${_rm}"' in ep and "for _rm in bin usr lib etc proc dev" in ep,
          "la limpieza del chroot es lista explicita con guardia :?")
    for cliente in ("upload", "private"):
        lista = ep.split("for _rm in")[1].split("\n")[0]
        check(cliente not in lista,
              f"{cliente}/ no cae en la lista de borrado del chroot")
    check('mkdir -p "${SFTP_CHROOT}/dev"' not in ep and "mknod" not in ep,
          "no se crean device nodes (medido: internal-sftp no los necesita)")
    check('rmdir "${SFTP_CHROOT}/tmp"' in ep and
          'SFTP_CHROOT}/tmp"' not in ep.replace('rmdir "${SFTP_CHROOT}/tmp"', ""),
          "/tmp no se crea; el de un proyecto viejo se quita con rmdir")
    check("PRIVATEDIR" in ep and "chmod 700" in ep,
          "el dir privado se crea con mkdir -p y queda 700")
    check("standalone" in ep and ".next/standalone" in ep,
          "el standalone de Next se copia a /app (si no, server.js no existe)")

    # ---- 8. el smoke y el resumen saben que hay una API ----
    print("\n=== smoke y resumen del developer ===")
    sys.path.insert(0, str(ROOT / "lib"))
    import smoke  # noqa: E402
    check(hasattr(smoke, "app_serves_python"),
          "smoke.app_serves_python existe")
    src = inspect.getsource(smoke.all_checks)
    check("app_serves_python" in src and 'cfg.get("stack")' in src,
          "all_checks corre el check de la API cuando el stack tiene python")
    sumsrc = leer("lib/summary.py")
    check(re.search(r'if "python" in stack:', sumsrc) is not None,
          "el resumen del developer tiene la seccion de la API")
    check("/api/" in sumsrc, "el resumen dice por donde entra la API")
    create = leer("bin/appctl")
    check('"stack": stack' in create,
          "create le pasa el stack al smoke (sin eso el check de la API no corre)")

    # ---- 9. los README lo documentan ----
    print("\n=== los README ===")
    for readme in ("README.md", "README.en.md"):
        t = leer(readme)
        check(NOMBRE in t, f"{readme} nombra el stack")
        check("/api" in t, f"{readme} documenta la ruta de la API")

    # ---- 10. el conteo de stacks del README ----
    reales = sorted(d.name for d in (ROOT / "stacks").iterdir() if d.is_dir())
    m = re.search(r"Hay \*\*(\d+)\*\*", leer("README.md"))
    if m:
        check(int(m.group(1)) == len(reales),
              f"el README dice {m.group(1)} stacks y hay {len(reales)}")

    # ---- 11. el check de la API, en los dos modos (sin docker) ----
    # create exige el stub; upgrade tiene que aceptar la API del cliente.
    # Con curl de verdad esto necesita un contenedor; con subprocess.run
    # parcheado se prueban las dos ramas y el parsing de la respuesta.
    print("\n=== el check de la API: stub vs codigo del cliente ===")

    class _Fake:
        def __init__(self, body, rc=0, code=200):
            # El smoke llama a curl con -w "\n%{http_code}": el stdout es el
            # cuerpo y despues el estado en su propia linea. Un fake que
            # devuelve solo el cuerpo hace que el estado se lea del cuerpo.
            self.returncode = rc
            self.stdout = f"{body}\n{code}"
            self.stderr = ("curl: (22) The requested URL returned error: 502"
                           if rc else "")

    import subprocess
    original = subprocess.run
    try:
        subprocess.run = lambda *a, **k: _Fake("app: python ok\npython: 3.14.8\nfastapi: 0.142.2\n")
        c = smoke.app_serves_python("/tmp", 8001, {}, expect_stub=True)
        check(c.ok, "con el stub y expect_stub=True: PASS")
        c = smoke.app_serves_python("/tmp", 8001, {}, expect_stub=False)
        check(c.ok, "con el stub y expect_stub=False: PASS")

        subprocess.run = lambda *a, **k: _Fake('{"hola":"cliente"}\n')
        c = smoke.app_serves_python("/tmp", 8001, {}, expect_stub=False)
        check(c.ok, "con la API del cliente y expect_stub=False: PASS")
        c = smoke.app_serves_python("/tmp", 8001, {}, expect_stub=True)
        check(not c.ok,
              "con la API del cliente y expect_stub=True: FAIL (es el caso "
              "que dejaba clavado a un proyecto con codigo real)")

        # uvicorn caido: nginx contesta 502 y `curl -f` sale != 0. Esto es lo
        # que tiene que fallar SIEMPRE, con o sin expect_stub.
        subprocess.run = lambda *a, **k: _Fake("", 22)
        c = smoke.app_serves_python("/tmp", 8001, {}, expect_stub=False)
        check(not c.ok, "con uvicorn caido (502 de nginx, curl != 0): FAIL")
        check("502" in c.detail or "curl" in c.detail.lower(),
              "y el detalle dice que fallo curl, no que la API contesto mal")
    finally:
        subprocess.run = original

    # ---- 12. HOME escribible para los programas que corren como app ----
    # El entrypoint corre como root, asi que los programas de supervisord
    # heredan HOME=/root (0700). Para un proceso corriendo como `app`, eso
    # es un HOME ilegible: pnpm muere con "Permission denied (os error 13)"
    # y el stub imprimia "pnpm: no" con pnpm instalado y andando. Medido:
    # HOME=/root -> falla; HOME=/home/app -> 12.10.1.
    print("\n=== HOME de los programas que no corren como root ===")
    for stack in ("nextjs-postgres-sftp", NOMBRE):
        sup_t = leer(f"stacks/{stack}/image/supervisord.conf")
        for prog in re.finditer(r"\[program:([a-z]+)\]\n(.*?)(?=\n\[|\Z)",
                                sup_t, re.S):
            nombre, cuerpo = prog.group(1), prog.group(2)
            if "user=app" not in cuerpo:
                continue
            # el environment puede estar partido en varias lineas (los
            # programas de node lo estan): hay que leer la continuacion
            # indentada, no solo la primera linea.
            env = re.search(r"environment=(.*(?:\n[ \t]+.*)*)", cuerpo)
            env = env.group(1) if env else ""
            check('HOME="/home/app"' in env,
                  f"{stack}: el programa {nombre} (user=app) tiene HOME escribible")

    print("\ntodo bien")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\nFALLA: {exc}")
        sys.exit(1)
