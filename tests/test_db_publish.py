#!/usr/bin/env python3
"""El puerto publicado de la DB: se persiste, se muestra y no se abre de mas.

QUE VERIFICA
  1. `db expose` publica en 0.0.0.0 por defecto: exponer es el proposito del
     comando (publicar solo en 127.0.0.1 obliga a un tunel ssh). `--bind`
     acepta una IP, no una red: medido, docker rechaza un CIDR en `ports:`
     ('-p 10.20.0.0/16:15999:80' -> exit 125).
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
import inspect
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

    # ---- 1. el bind: 0.0.0.0 por defecto, una IP se acepta, una red no ----
    print("\n=== 1. donde se publica ===")
    txt = appctl._render_publish(15432, "php-postgres-sftp")
    check("0.0.0.0:15432:5432" in txt,
          "el render publica en 0.0.0.0 por defecto (exponer es el proposito)")
    check("127.0.0.1:15432:5432" in
          appctl._render_publish(15432, "php-postgres-sftp", "127.0.0.1"),
          "y --bind 127.0.0.1 lo deja solo para este host (caso tunel)")
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
    check(estado == "ok" and visto.get("bind") == "0.0.0.0",
          "`db expose 15432` parsea con bind 0.0.0.0 (el default nuevo)")
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
                      .replace("[--bind IP]", "--bind 0.0.0.0"))
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
        # el BIND viaja con el puerto: un render que lo ignore cambia 0.0.0.0
        # por 127.0.0.1 y le corta el acceso al cliente que entra de la red
        d2 = tmp / "bind"
        appctl.render_project(d2, stack_dir, "php-postgres-sftp", "t", comps,
                              {"php": "8.5-fpm-alpine"}, names, pw, 18001, 12201,
                              host="t.local", pg_major="18", api_key="k",
                              db_publish=15432, db_publish_bind="0.0.0.0")
        db2 = bloque_db((d2 / "compose.yaml").read_text(encoding="utf-8"))
        check('"0.0.0.0:15432:5432"' in db2,
              "el render respeta el bind del state (0.0.0.0, no el default)")
        check('"127.0.0.1:15432:5432"' not in db2,
              "y NO lo pisa con 127.0.0.1")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- 4b. el puerto que YA esta en el compose se respeta (y se anota) ----
    print("\n=== 4b. compatibilidad: proyectos expuestos con la version vieja ===")
    con_puerto = (
        "services:\n"
        "  app:\n"
        "    image: x\n"
        "    ports:\n"
        "      - \"8001:80\"\n"
        "      - \"2221:22\"\n"
        "  db:\n"
        "    image: postgres\n"
        "    ports:\n"
        "      - \"0.0.0.0:5434:5432\"\n"
        "    networks:\n"
        "      - data\n"
        "      - egress\n"
        "  data:\n"
        "    driver: bridge\n")
    sin_puerto = (
        "services:\n"
        "  app:\n"
        "    image: x\n"
        "    ports:\n"
        "      - \"8001:80\"\n"
        "  db:\n"
        "    image: postgres\n"
        "    networks:\n"
        "      - data\n")
    leido = appctl._publish_desde_compose(con_puerto)
    check(leido == ("0.0.0.0", 5434),
          f"lee el puerto publicado del compose (0.0.0.0:5434) -> {leido}")
    check(appctl._publish_desde_compose(sin_puerto) is None,
          "sin ports: en el db, no inventa un puerto")
    check(appctl._publish_desde_compose(sin_puerto) is None,
          "NO confunde el ports: de la app con el de la db")
    check(appctl._publish_desde_compose(
              con_puerto.replace('0.0.0.0:5434', '127.0.0.1:15932'))
          == ("127.0.0.1", 15932),
          "lee tambien el bind, no solo el puerto")
    src_up = inspect.getsource(appctl.cmd_upgrade)
    check("_publish_desde_compose(before)" in src_up and "db_published_bind" in src_up,
          "upgrade lo lee del compose y lo ANOTA en el state (no lo cierra)")
    check("db_publish_bind=" in src_up,
          "upgrade le pasa el bind al render (no solo el puerto)")
    check("save_state(project, st)" in src_up and "_anotado" in src_up,
          "el puerto descubierto en el compose se PERSISTE con --yes")


    # ---- 5. upgrade: el puerto del state llega al render ----
    print("\n=== 5. upgrade no pierde el puerto ===")
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
    check("t.local:15432" in out and "cualquier red" in out,
          "abierta a todo: muestra el host UTIL (no el 0.0.0.0) y el alcance")
    check("0.0.0.0:15432" not in out,
          "y no muestra 0.0.0.0, que no es una direccion a la que nadie conecte")
    out = summary.render_info(st, {"SFTP_USER": "t"}, ps, None, "127.0.0.1:15432")
    check("127.0.0.1:15432" in out and "de afuera no entra" in out,
          "solo local: NO muestra el FQDN (mandaria al cliente a una direccion "
          "que no lo atiende) y avisa que de afuera no entra")

    # ---- 7. `creds` tambien lo dice (antes estaba hardcodeado al reves) ----
    print("\n=== 7. `creds` dice si la base esta publicada ===")
    nombres = {"DB_NAME": "t_db", "DB_USER": "t", "DB_MIGRATION_USER": "t_mig",
               "DB_READONLY_USER": "t_ro", "SFTP_USER": "t"}
    pw = {"DB_PASSWORD": "x", "DB_MIGRATION_PASSWORD": "y",
          "DB_READONLY_PASSWORD": "z", "SFTP_PASSWORD": "s"}
    base = summary.render_create("t", "php-postgres-sftp", "t.local", 18001, 12201,
                                 nombres, pw, "/tmp/t", creds_only=True)
    check("no esta publicada" in base,
          "sin publicar: creds dice que la base no esta publicada")
    abierto = summary.render_create("t", "php-postgres-sftp", "t.local", 18001,
                                    12201, nombres, pw, "/tmp/t", creds_only=True,
                                    db_publish="0.0.0.0:5435")
    check("t.local:5435" in abierto and "publicada" in abierto.lower(),
          "publicada en 0.0.0.0: creds la muestra en el FQDN")
    local = summary.render_create("t", "php-postgres-sftp", "t.local", 18001,
                                  12201, nombres, pw, "/tmp/t", creds_only=True,
                                  db_publish="127.0.0.1:5435")
    check("127.0.0.1:5435" in local and "De afuera NO entra" in local,
          "publicada solo local: lo dice y explica como abrirla")
    check("--bind 0.0.0.0" in local,
          "y da el comando exacto para que el cliente entre")
    cli = (ROOT / "bin" / "appctl").read_text()
    check("db_publish=_db_publish_vivo(args.proyecto)" in cli,
          "cmd_creds consulta la verdad de docker (no el state)")
    check("SOLO en este host" in cli and "cualquier red que" in cli,
          "db expose avisa el alcance al publicar (local y abierto)")

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
