#!/usr/bin/env python3
"""El componente `s3` (Garage): un S3 por proyecto, para cualquier stack.

QUE VERIFICA
  - que `s3` sea un COMPONENTE (como `cron`), no un stack: va con los 6, no
    cambia la imagen ni la DB
  - que los 6 templates tengan los MARCADORES (__S3__ y __S3_APP_ENV__) y no el
    bloque literal: si el bloque estuviera escrito en el template, todos los
    proyectos tendrian Garage aunque no lo pidan
  - que render_project lo arme de verdad: el servicio garage, el env de la app,
    el garage.toml (0600, con los secretos) y los directorios de datos
  - y que SIN s3 no aparezca nada de eso
  - que el init hable con el CLI de Garage con el ORDEN correcto: el node id
    PRIMERO y -t al final (-t es multi-valor y se come el node id: medido)
  - que el layout se aplique UNA sola vez (version 0 -> aplicar; >=1 -> no):
    aplicar dos veces la misma version es lo que las docs dicen que no hay que
    hacer
  - que los 4 checks existan y se despachen por COMPONENTE, no por stack
  - que el resumen le diga al developer donde esta el S3 y con que credenciales
  - que este documentado en los dos READMEs
  - que no haya ningun rm -rf sobre datos del cliente (regla dura del repo)

QUE NO VERIFICA: que Garage arranque ni que un cliente S3 funcione. Eso es E2E
con docker (create real + operacion firmada con boto3).

Uso: python3 tests/test_s3_component.py
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import re
import sys
import tempfile

RAIZ = pathlib.Path(__file__).resolve().parent.parent
STACKS = ["express-postgres-sftp", "nextjs-postgres-sftp", "nextjs-python-postgres-sftp",
          "php-mysql-sftp", "php-postgres-sftp", "tomcat-postgres-sftp"]
FALLOS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  {'PASS' if cond else 'FALLA'}  {msg}")
    if not cond:
        FALLOS.append(msg)


def leer(rel: str) -> str:
    return (RAIZ / rel).read_text(encoding="utf-8")


def cargar_cli():
    loader = importlib.machinery.SourceFileLoader("appctl", str(RAIZ / "bin/appctl"))
    spec = importlib.util.spec_from_loader("appctl", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def main() -> int:
    cli = cargar_cli()
    app = leer("bin/appctl")

    print("\n=== 1. s3 es un componente, no un stack ===")
    check("s3" in cli.ALL_COMPONENTS, "s3 esta en ALL_COMPONENTS")
    check("s3" not in cli.RUNTIMES, "s3 NO es un runtime (va con cualquiera)")
    check("Componentes: php | nextjs | tomcat | python | psql | mysql | sftp | cron | s3"
          in app, "el epilogo lo lista")
    check("s3 es un complemento" in app, "el epilogo explica que es un complemento")
    check('"garage": os.environ.get("APPCTL_GARAGE_TAG", "v2.4.1")' in app,
          "el tag de garage se pinea en DEFAULT_TAGS")

    print("\n=== 2. los 6 templates: marcadores, no el bloque literal ===")
    for st in STACKS:
        t = leer(f"stacks/{st}/compose.tmpl.yaml")
        check("__S3__\n" in t, f"{st}: tiene el marcador __S3__")
        check("__S3_APP_ENV__\n" in t, f"{st}: tiene el marcador __S3_APP_ENV__")
        check("  garage:\n" not in t,
              f"{st}: NO tiene el servicio garage escrito (si no, va en todos lados)")

    print("\n=== 3. render_project: con s3 y sin s3 ===")
    names = {"DB_NAME": "acme_db", "DB_USER": "acme", "DB_MIGRATION_USER": "acme_mig",
             "DB_READONLY_USER": "acme_ro", "SFTP_USER": "acme",
             "S3_ACCESS_KEY_ID": cli.sec.s3_access_key(), "S3_BUCKET": "acme"}
    pw = {"DB_ADMIN_PASSWORD": "a", "DB_PASSWORD": "b", "DB_MIGRATION_PASSWORD": "c",
          "DB_READONLY_PASSWORD": "d", "SFTP_PASSWORD": "e", "APP_KEY": "base64:x",
          "S3_SECRET_ACCESS_KEY": cli.sec.hex64(), "S3_RPC_SECRET": cli.sec.hex64(),
          "S3_ADMIN_TOKEN": cli.sec.b64_32(), "S3_METRICS_TOKEN": cli.sec.b64_32()}
    check(names["S3_ACCESS_KEY_ID"].startswith("GK") and len(names["S3_ACCESS_KEY_ID"]) == 26,
          "la access key es GK + 24 hex (el formato que acepta `garage key import`)")
    check(len(pw["S3_SECRET_ACCESS_KEY"]) == 64, "el secret es de 64 hex")
    import yaml
    # el create solo mete las claves S3 en names/pw cuando pidio s3: el test
    # tiene que hacer lo mismo, o mide un .env que en la vida real no existe.
    names_no = {k: v for k, v in names.items() if not k.startswith("S3_")}
    pw_no = {k: v for k, v in pw.items() if not k.startswith("S3_")}
    for want in (True, False):
        d = pathlib.Path(tempfile.mkdtemp())
        cli.render_project(d, cli.STACKS_DIR / "php-postgres-sftp", "php-postgres-sftp", "acme",
                           {"php", "psql", "sftp", "s3"} if want else {"php", "psql", "sftp"},
                           {"php": "8.5-fpm-alpine", "psql": "18-alpine", "garage": "v2.4.1"},
                           names if want else names_no, pw if want else pw_no,
                           8001, 2221, host="h", pg_major="18", api_key="k",
                           want_cron=False, want_s3=want)
        svcs = yaml.safe_load((d / "compose.yaml").read_text())["services"]
        if want:
            check("garage" in svcs, "con s3: el servicio garage esta en el compose")
            check(svcs["garage"]["image"] == "dxflrs/garage:v2.4.1",
                  "con s3: la imagen es la oficial, con el tag pinneado")
            check(svcs["garage"]["container_name"] == "acme_garage",
                  "con s3: el contenedor se llama <proyecto>_garage")
            check(svcs["garage"]["healthcheck"]["test"][-1] == "status",
                  "con s3: el healthcheck usa `garage status` (no un puerto a ciegas)")
            check("garage" in svcs["app"]["environment"].get("S3_ENDPOINT", "") or
                  svcs["app"]["environment"].get("S3_ENDPOINT") == "http://garage:3900",
                  "con s3: la app recibe S3_ENDPOINT por la red interna")
            check(svcs["app"]["environment"].get("AWS_ACCESS_KEY_ID") is not None,
                  "con s3: tambien van los nombres de AWS (los SDK los toman solos)")
            check((d / "s3" / "data").is_dir() and (d / "s3" / "meta").is_dir(),
                  "con s3: los directorios de datos viven en el proyecto")
            toml = (d / "s3" / "garage.toml").read_text()
            check('metadata_dir = "/meta"' in toml and 'data_dir = "/data"' in toml,
                  "con s3: el garage.toml apunta a los volumenes")
            check("replication_factor = 1" in toml, "con s3: single node (sin replicacion)")
            rpc = [l for l in toml.splitlines() if l.startswith("rpc_secret")][0]
            check(len(rpc.split('"')[1]) == 64, "con s3: el rpc_secret es de 64 hex")
            check((d / "s3" / "garage.toml").stat().st_mode & 0o777 == 0o600,
                  "con s3: el garage.toml queda en 0600 (tiene secretos)")
            env = (d / ".env").read_text()
            for k in ("S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY",
                      "S3_RPC_SECRET", "S3_ADMIN_TOKEN"):
                check(f"{k}=" in env, f"con s3: {k} esta en el .env")
            check((d / ".env").stat().st_mode & 0o777 == 0o600, "con s3: el .env sigue en 0600")
            check('"access_key_id"' in (d / "state.json").read_text(),
                  "con s3: el state guarda el bucket y la key")
        else:
            check("garage" not in svcs, "sin s3: no hay servicio garage")
            check(not (d / "s3").exists(), "sin s3: no hay directorio s3")
            check("S3_BUCKET" not in (d / ".env").read_text(), "sin s3: no hay S3_* en el .env")
            check('"s3": null' in (d / "state.json").read_text(), "sin s3: el state dice null")

    print("\n=== 4. el init: orden de argumentos y una sola aplicacion del layout ===")
    init = app[app.find("def _s3_init("):app.find("def _s3_init(") + 4200]
    check('g("layout", "assign", node, "-z", "dc1", "-c", cap, "-t", "appctl")' in init,
          "el node id va PRIMERO y -t al final (-t es multi-valor: se come el node id)")
    check('Current cluster layout version: (\\d+)' in init or "Current cluster layout version" in init,
          "lee la version del layout de `layout show`")
    check('if int(m.group(1)) == 0:' in init,
          "solo aplica el layout si esta en 0 (aplicar dos veces la misma version = no)")
    check('g("layout", "apply", "--version", "1")' in init, "aplica la version 1")
    check('"key", "import", "--yes", "-n", project' in init,
          "importa la key con `garage key import` (el secreto lo genera appctl)")
    check('"bucket", "allow"' in init and "--read" in init and "--write" in init,
          "da permisos de lectura y escritura sobre el bucket")
    check('"exec"' in init and "/garage" in init,
          "habla por docker exec: la imagen es scratch, no hay shell para un sidecar")
    # sh() corre con shell=True y recibe un STRING: pasarle una lista hace que
    # el shell se coma los argumentos y el comando falle en silencio. En el E2E
    # eso se vio como un init esperando 300s un `status` que nunca corrio.
    check('" ".join(sh_quote(' in init,
          "el comando se arma como STRING citado (sh() usa shell=True)")
    check("sh(base +" not in init, "no le pasa listas a sh()")

    print("\n=== 5. los checks: por componente, no por stack ===")
    smoke = leer("lib/smoke.py")
    for f in ("s3_garage_listo", "s3_la_app_lo_alcanza", "s3_bucket_y_key", "s3_creds_en_la_app"):
        check(f"def {f}(" in smoke, f"existe {f}")
    check('if "s3" in (cfg.get("components") or []):' in smoke,
          "el despacho es por COMPONENTE (s3 va con los 6 stacks)")
    check('["garage"] if "s3" in (cfg.get("components") or []) else []' in smoke,
          "wait_healthy tambien espera al garage")
    check("layout esta en version 0" in smoke,
          "el check explica el caso 'Garage sin layout' (500 en todo, healthcheck verde)")

    print("\n=== 6. el resumen le dice al developer donde y con que ===")
    summ = leer("lib/summary.py")
    check("S3 (Garage)" in summ and "Endpoint   http://garage:3900" in summ,
          "el resumen muestra el endpoint interno")
    check("S3_ACCESS_KEY_ID" in summ and "S3_SECRET_ACCESS_KEY" in summ,
          "y las credenciales")
    check("path-style" in summ, "avisa que es path-style (el SDK de JS necesita forcePathStyle)")

    print("\n=== 7. el subcomando s3 ===")
    for sub in ('s3sub.add_parser("init"', 's3sub.add_parser("status"', 's3sub.add_parser("garage"'):
        check(sub in app, f"existe el subcomando {sub.split('(')[1]}")
    check("def cmd_s3_init(" in app and "def cmd_s3_status(" in app and "def cmd_s3_garage(" in app,
          "los tres comandos estan implementados")
    check('choices=["app", "db", "garage"]' in app, "`limits --service` conoce el garage")

    print("\n=== 8. documentado en los dos READMEs ===")
    for n in ("README.md", "README.en.md"):
        t = leer(n)
        check("s3" in t.lower() and "garage" in t.lower(), f"{n} menciona el s3/garage")
        check("appctl acme nextjs python psql sftp s3" in t or "s3" in t,
              f"{n} muestra como se pide")

    print("\n=== 8b. sh() recibe STRING (shell=True), nunca una lista ===")
    # El bug que costo el E2E: sh() corre con shell=True y recibe un STRING. Con
    # una lista, el shell se queda con el primer elemento y le pasa el resto
    # como SUS argumentos: el comando no corre y falla en silencio (un init
    # esperando 300s, un status diciendo "no corre" con el contenedor sano).
    check("sh([" not in app, "bin/appctl no le pasa listas a sh()")
    check('" ".join(sh_quote(' in app, "los comandos con argumentos se arman citados")

    print("\n=== 9. la regla dura: ningun rm -rf sobre datos del cliente ===")
    for rel in ("bin/appctl", "lib/summary.py", "lib/smoke.py"):
        t = leer(rel)
        for m in re.finditer(r"rm -rf[^\n]*", t):
            linea = m.group(0)
            check(not re.search(r"(s3|upload|data|meta)", linea),
                  f"{rel}: el rm -rf no toca datos del cliente -> {linea[:70]}")

    print()
    if FALLOS:
        print(f"{len(FALLOS)} fallo(s):")
        for f in FALLOS:
            print(f"  - {f}")
        return 1
    print("OK: el componente s3 esta completo y cableado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
