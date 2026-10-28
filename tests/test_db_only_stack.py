#!/usr/bin/env python3
"""Proyecto sólo-base: `appctl create midb psql`.

Un stack `*-only` es la base sola: un único servicio, sin app, sin nginx, sin
SFTP. Este test cubre el nombre del stack, lo que se rechaza, el compose que se
renderiza (válido YAML, cerrado y expuesto) y el resumen sin app.
"""
import pathlib
import re
import subprocess
import sys

RAIZ = pathlib.Path(__file__).resolve().parent.parent
fallos = []


def check(ok, msg, detalle=""):
    if not ok:
        fallos.append(msg)
        print(f"  FAIL - {msg}" + (f"  [{detalle}]" if detalle else ""))
    else:
        print(f"  ok   - {msg}")


def cli(code: str) -> str:
    """Corre un fragmento con el CLI cargado (sin ejecutar main)."""
    preludio = f'''
import importlib.machinery, importlib.util, sys, io, contextlib
ARGS = sys.argv[1:]
loader = importlib.machinery.SourceFileLoader("appctl", {str(RAIZ / "bin" / "appctl")!r})
spec = importlib.util.spec_from_loader("appctl", loader)
m = importlib.util.module_from_spec(spec)
sys.argv = ["appctl"]
loader.exec_module(m)
buf, err = io.StringIO(), io.StringIO()
try:
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
{code}
except SystemExit:
    pass
print((err.getvalue() + buf.getvalue()).strip())
'''
    r = subprocess.run([sys.executable, "-c", preludio, *[]],
                       capture_output=True, text=True, timeout=120)
    return r.stdout + r.stderr


# ---- 1. el nombre del stack
print("=== stack_name")
casos = [(["psql"], "postgres-only"), (["mysql"], "mysql-only")]
for comps, esperado in casos:
    out = cli(f'        print(m.stack_name({set(comps)!r}))').strip().splitlines()
    obtenido = out[-1] if out else ""
    check(obtenido == esperado, f"{comps} -> {esperado}", obtenido)

# ---- 2. lo que NO se acepta
print("\n=== se rechaza")
for comps, aguja in ((["psql", "sftp"], "no hay app que lo use"),
                     (["mysql", "cron"], "no hay app que lo use"),
                     (["psql", "s3"], "no hay app que lo use"),
                     (["psql", "mysql"], "no los dos"),
                     (["psql", "python"], "python")):
    out = cli(f'        print(m.stack_name({set(comps)!r}))')
    check(aguja in out and "Traceback" not in out,
          f"{comps} explica ({aguja})", out.strip().splitlines()[-1][:70] if out.strip() else "sin salida")

# ---- 3. el compose que se renderiza
print("\n=== compose (YAML, cerrado y expuesto)")
import tempfile
try:
    import yaml
except ImportError:
    yaml = None

for stack, motor, puerto in (("postgres-only", "postgres", "5432"),
                             ("mysql-only", "mariadb", "3306")):
    tmpl = (RAIZ / "stacks" / stack / "compose.tmpl.yaml").read_text(encoding="utf-8")
    # ojo: la palabra "nginx" aparece en el comentario de cabecera ("no hay
    # nginx"); lo que importa es que no haya SERVICIO app ni imagen de app.
    check("\n  app:" not in tmpl and "image: appctl/" not in tmpl,
          f"{stack}: el template no tiene servicio app")
    check(f"image: {motor}:" in tmpl, f"{stack}: usa la imagen de {motor}")
    check("__DB_PUBLISH__" in tmpl, f"{stack}: tiene el marcador de publicacion")
    check((RAIZ / "stacks" / stack / "init").is_dir(),
          f"{stack}: trae init/ (los tres roles)")
    if yaml:
        for etiqueta, pub in (("cerrado", ""),
                              ("expuesto", f'    ports:\n      - "0.0.0.0:5439:{puerto}"')):
            s = tmpl.replace("__PROYECTO__", "demo")
            s = s.replace("__PG_VERSION__", "18").replace("__MYSQL_VERSION__", "11.8")
            s = re.sub(r"\$\{[A-Z_]+:-[^}]*\}", "x", s)
            s = re.sub(r"\$\{[A-Z_]+\}", "x", s)
            s = s.replace("__DB_PUBLISH__", pub)
            try:
                doc = yaml.safe_load(s)
            except Exception as e:
                check(False, f"{stack}: YAML valido ({etiqueta})", str(e)[:80])
                continue
            check(list(doc["services"]) == ["db"],
                  f"{stack}: un solo servicio (db) [{etiqueta}]", str(list(doc["services"])))
            check("ports" not in doc["services"]["db"] if etiqueta == "cerrado"
                  else "ports" in doc["services"]["db"],
                  f"{stack}: {etiqueta} {'sin' if etiqueta == 'cerrado' else 'con'} puerto")

# ---- 4. el resumen no habla de app ni de SFTP
print("\n=== resumen")
sys.path.insert(0, str(RAIZ / "lib"))
import summary  # noqa: E402
n = {"DB_NAME": "midb_db", "DB_USER": "midb", "DB_MIGRATION_USER": "midb_mig",
     "DB_READONLY_USER": "midb_ro", "SFTP_USER": "midb"}
p = {"DB_PASSWORD": "pw1", "DB_MIGRATION_PASSWORD": "pw2",
     "DB_READONLY_PASSWORD": "pw3", "SFTP_PASSWORD": "pw4"}
txt = summary.render_create("midb", "postgres-only", "db.ejemplo.com", None, None,
                            n, p, "/srv/appctl/midb")
check("Acceso a la aplicacion" not in txt, "el resumen no ofrece URL de app")
check("/sftp/home/upload" not in txt, "el resumen no manda a subir por SFTP")
check("db expose" in txt, "el resumen dice como abrir la base")
check("midb_db" in txt and "pw1" in txt, "el resumen trae base y credenciales")

# ---- 5. la puerta real: proyecto inexistente con `psql` de segundo token
print("\n=== appctl <proj> psql en un proyecto que no existe")
r = subprocess.run([sys.executable, str(RAIZ / "bin" / "appctl"),
                    "un-proyecto-que-no-existe-xyz", "psql"],
                   capture_output=True, text=True,
                   env=dict(__import__("os").environ, PYTHONPATH=str(RAIZ / "lib")),
                   timeout=120)
out = r.stdout + r.stderr
check("no existe el proyecto" in out and "appctl create" in out,
      "dice que no existe y como se crea una base sola")

# ---- 6. el puerto automatico no se queda con el nativo
print("\n=== db expose sin puerto")
cli_txt = (RAIZ / "bin" / "appctl").read_text(encoding="utf-8")
check("_puerto_libre(desde + 1)" in cli_txt,
      "el puerto automatico arranca en el siguiente del nativo (no en 5432/3306)")

print()
print(f"test_db_only_stack: {'OK' if not fallos else str(len(fallos)) + ' FALLAS'}")
sys.exit(1 if fallos else 0)
