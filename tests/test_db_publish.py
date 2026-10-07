#!/usr/bin/env python3
"""El puerto publicado de la DB: se persiste, se muestra y no se abre de mas.

QUE VERIFICA
  1. `db expose` publica en 127.0.0.1 por defecto (lo que el README promete)
     y `--bind` acepta una IP, no una red: medido, docker rechaza un CIDR en
     `ports:` ('-p 10.20.0.0/16:15999:80' -> exit 125).
  2. El render del proyecto respeta el puerto persistido, y publicar mete al
     db en la red `egress`: su unica red (`data`) es internal:true y docker
     NO publica un puerto de ahi, asi que sin ese paso el puerto queda
     anunciado pero inerte.
  3. `upgrade` pasa el puerto del state al render. Esa es la regresion que
     hacia que el puerto se perdiera en silencio: vivia solo en el compose
     renderizado y el proximo re-render lo borraba.
  4. `info` dice si esta publicada (con la verdad de docker) y avisa cuando
     el state declara un puerto que el contenedor no publica.
  5. Los ejemplos de `db expose` del README son comandos que EXISTEN: cada
     uno se parsea con el parser real del CLI. El README documentaba
     `--cidr` para expose, que no existia.

QUE NO VERIFICA: que el puerto responda de verdad y que sobreviva a un
upgrade real. Eso es E2E con docker (create + expose + upgrade + nc).
"""
from __future__ import annotations

import importlib.util
import re
import shutil
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FALLOS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  PASS  {msg}")
    else:
        print(f"  FAIL  {msg}")
        FALLOS.append(msg)


def cargar_appctl():
    """El bin/appctl real, importado como modulo (no es un .py)."""
    sys.path.insert(0, str(ROOT / "lib"))
    loader = SourceFileLoader("appctl_publish_test", str(ROOT / "bin" / "appctl"))
    spec = importlib.util.spec_from_loader("appctl_publish_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def correr_cli(mod, argv):
    """Corre main() con un cmd_db_expose falso: prueba el PARSER, no docker.

    Sin el falso, `db expose` abriria un puerto de verdad al parsear. main()
    no hace nada antes de despachar a args.func, asi que reemplazarlo
    alcanza para verificar que argumentos llegaron.
    """
    visto: dict = {}

    def falso(args):
        visto["bind"] = getattr(args, "bind", None)
        visto["puerto"] = args.puerto
        return 0

    original = mod.cmd_db_expose
    mod.cmd_db_expose = falso
    viejo = sys.argv
    try:
        sys.argv = argv
        try:
            rc = mod.main()
        except SystemExit as e:
            return ("exit", e.code), visto
        return ("ok", rc), visto
    finally:
        mod.cmd_db_expose = original
        sys.argv = viejo


def bloque_db(compose: str) -> str:
    """El texto del servicio `db:` del compose (y no el resto)."""
    lineas = compose.splitlines()
    out, dentro = [], False
    for l in lineas:
        if l == "  db:":
            dentro = True
            out.append(l)
            continue
        if dentro and l and not l.startswith(" "):
            break
        if dentro:
            out.append(l)
    return "\n".join(out)


def main() -> int:
    print("Puerto publicado de la DB: persistencia, info y bind")
    appctl = cargar_appctl()

    # ---- 1. el bind: 127.0.0.1 por defecto, una IP se acepta, una red no ----
    print("\n=== 1. donde se publica ===")
    txt = appctl._render_publish(15432, "php-postgres-sftp")
    check("127.0.0.1:15432:5432" in txt,
          "el render publica en 127.0.0.1 (no en 0.0.0.0)")
    check("0.0.0.0" not in txt,
          "el render NO publica en 0.0.0.0 por defecto")
    check("3306" in appctl._render_publish(15432, "php-mysql-sftp"),
          "el puerto interno lo decide el stack (mariadb: 3306)")
    check("10.20.0.5:15432:5432" in
          appctl._render_publish(15432, "php-postgres-sftp", "10.20.0.5"),
          "--bind cambia la direccion publicada")
    for malo in ("10.20.0.0/16", "no-es-ip", "999.1.1.1", "127.0.0.1:5432"):
        try:
            appctl._validar_bind(malo)
            check(False, f"_validar_bind rechaza {malo!r}")
        except SystemExit:
            check(True, f"_validar_bind rechaza {malo!r} (docker no acepta una red ahi)")
    check(appctl._validar_bind("0.0.0.0") == "0.0.0.0",
          "_validar_bind acepta 0.0.0.0 si alguien lo pide a proposito")

    # ---- 2. el parser: --bind existe, --cidr es de `db grant` ----
    print("\n=== 2. el parser ===")
    (estado, _), visto = correr_cli(appctl, ["appctl", "dummy", "db", "expose", "15432"])
    check(estado == "ok" and visto.get("bind") == "127.0.0.1",
          "`db expose 15432` parsea con bind 127.0.0.1")
    (estado, _), visto = correr_cli(appctl, ["appctl", "dummy", "db", "expose", "15432",
                                             "--bind", "10.20.0.5"])
    check(estado == "ok" and visto.get("bind") == "10.20.0.5",
          "`db expose 15432 --bind IP` parsea")
    (estado, code), _ = correr_cli(appctl, ["appctl", "dummy", "db", "expose", "15432",
                                            "--cidr", "10.20.0.0/16"])
    check(estado == "exit" and code == 2,
          "`expose --cidr` NO existe (argparse lo rechaza; --cidr es de grant)")

    # ---- 3. los ejemplos del README tienen que existir ----
    print("\n=== 3. los ejemplos de `db expose` del README ===")
    vistos = 0
    for doc in ("README.md", "README.en.md"):
        for linea in (ROOT / doc).read_text(encoding="utf-8").splitlines():
            s = linea.strip()
            if not s.startswith("appctl") or "db expose" not in s:
                continue
            cmd = re.split(r"\s+#", s, maxsplit=1)[0].strip()
            # Las lineas de referencia usan placeholders (<puerto>, [--bind IP]):
            # lo que se verifica son los FLAGS, asi que se los reemplaza por
            # valores reales en vez de saltear la linea.
            cmd = (cmd.replace("<puerto>", "15432").replace("<port>", "15432")
                      .replace("[--bind IP]", "--bind 127.0.0.1"))
            argv = [a.replace("<p>", "dummy").replace("<proyecto>", "dummy")
                    .replace("<project>", "dummy") for a in cmd.split()]
            (estado, code), _ = correr_cli(appctl, argv)
            check(estado == "ok", f"{doc}: existe -> {cmd}")
            vistos += 1
    check(vistos >= 4, f"se revisaron los ejemplos de los dos README ({vistos})")

    # ---- 4. el render respeta el puerto persistido ----
    print("\n=== 4. el render: puerto + red con gateway ===")
    stack_dir = ROOT / "stacks" / "php-postgres-sftp"
    names = {"DB_NAME": "t_db", "DB_USER": "t", "DB_MIGRATION_USER": "t_mig",
             "DB_READONLY_USER": "t_ro", "SFTP_USER": "t", "DB_ADMIN_USER": "t_admin"}
    pw = {"DB_ADMIN_PASSWORD": "a" * 24, "DB_PASSWORD": "b" * 24,
          "DB_MIGRATION_PASSWORD": "c" * 24, "DB_READONLY_PASSWORD": "d" * 24,
          "SFTP_PASSWORD": "e" * 20, "APP_KEY": "base64:" + "f" * 43 + "="}
    comps = {"php", "psql", "sftp"}
    tmp = Path(tempfile.mkdtemp(prefix="appctl-publish-test-"))
    try:
        for etiqueta, pub in (("cerrada", None), ("publicada", 15432)):
            d = tmp / etiqueta
            appctl.render_project(d, stack_dir, "php-postgres-sftp", "t", comps,
                                  {"php": "8.5-fpm-alpine"}, names, pw, 18001, 12201,
                                  host="t.local", pg_major="18", api_key="k",
                                  db_publish=pub)
            comp = (d / "compose.yaml").read_text(encoding="utf-8")
            db = bloque_db(comp)
            check("__" not in db.replace("__", "", 0) or "__DB_PUBLISH__" not in db,
                  f"{etiqueta}: sin placeholders sin sustituir en el db")
            if pub:
                check(f"127.0.0.1:{pub}:5432" in db,
                      f"{etiqueta}: el db publica el puerto del state")
                check("- egress" in db,
                      f"{etiqueta}: el db sale por egress (sin eso el puerto es inerte)")
            else:
                check("ports:" not in db,
                      f"{etiqueta}: el db no tiene ports:")
                check("- egress" not in db,
                      f"{etiqueta}: el db no tiene egress de mas")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- 5. upgrade: el puerto del state llega al render ----
    print("\n=== 5. upgrade no pierde el puerto ===")
    import inspect
    src = inspect.getsource(appctl.cmd_upgrade)
    check('db_publish=st.get("db_published_port")' in src,
          "cmd_upgrade pasa el puerto del state al render")
    src_ex = inspect.getsource(appctl.cmd_db_expose)
    check("save_state" in src_ex and "db_published_port" in src_ex,
          "expose ANOTA el puerto en el state")
    src_un = inspect.getsource(appctl.cmd_db_unexpose)
    check("db_published_port" in src_un and "save_state" in src_un,
          "unexpose borra la marca del state")
    check("shutil.copy2(bak, compose)" not in src_un,
          "unexpose NO restaura el backup viejo (seria volver a una imagen vieja)")

    # ---- 6. info lo muestra ----
    print("\n=== 6. `info` dice si esta publicada ===")
    sys.path.insert(0, str(ROOT / "lib"))
    import summary
    st = {"project": "t", "stack": "php-postgres-sftp", "host": "t.local",
          "app_port": 18001, "sftp_port": 12201, "components": ["php", "psql", "sftp"],
          "tags": {"php": "8.5-fpm-alpine"}, "db": {"name": "t_db"}}
    ps = "app\trunning\thealthy\ndb\trunning\thealthy\n"
    out = summary.render_info(st, {"SFTP_USER": "t"}, ps, None)
    check("red interna" in out, "sin publicar: dice red interna")
    out = summary.render_info(st, {"SFTP_USER": "t"}, ps, None, "127.0.0.1:15432")
    check("publicada en 127.0.0.1:15432" in out, "publicada: dice donde esta publicada")
    out = summary.render_info(st, {"SFTP_USER": "t"}, ps, None, "0.0.0.0:15432")
    check("0.0.0.0:15432" in out, "y si quedo abierta a todo, lo muestra")
    st2 = dict(st, db_published_port=15432)
    out = summary.render_info(st2, {"SFTP_USER": "t"}, ps, None)
    check("NO la publica" in out,
          "state y docker en desacuerdo: lo dice en vez de taparlo")

    print()
    if FALLOS:
        print(f"FALLARON {len(FALLOS)}:")
        for f in FALLOS:
            print(f"  - {f}")
        return 1
    print("OK: el puerto publicado se persiste, se muestra y no se abre de mas")
    return 0


if __name__ == "__main__":
    sys.exit(main())
