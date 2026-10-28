#!/usr/bin/env python3
"""Sin runtime y sin base: error claro, nunca un traceback.

Antes de esto, stack_name() explotaba con
TypeError: sequence item 0: expected str instance, NoneType found.
La base sola (psql|mysql) ahora ES valida: se cubre en test_db_only_stack.py.
"""
import pathlib
import subprocess
import sys

RAIZ = pathlib.Path(__file__).resolve().parent.parent
CARGAR = f'''
import importlib.machinery, importlib.util, sys, io, contextlib
ARGS = sys.argv[1:]
loader = importlib.machinery.SourceFileLoader("appctl", {str(RAIZ / "bin" / "appctl")!r})
spec = importlib.util.spec_from_loader("appctl", loader)
m = importlib.util.module_from_spec(spec)
sys.argv = ["appctl"]
loader.exec_module(m)
buf, err = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
    try:
        print(m.stack_name(set(ARGS)))
    except SystemExit:
        pass
print(err.getvalue() + buf.getvalue())
'''
DEVOLVER = CARGAR.replace('print(err.getvalue() + buf.getvalue())',
                          'print(m.stack_name(set(ARGS)))')

fallos = []

# sin runtime y sin base: tiene que explicar que falta el runtime
for comps in (["sftp"], ["cron"], ["sftp", "cron"]):
    r = subprocess.run([sys.executable, "-c", CARGAR, *comps],
                       capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    if "Traceback" in out:
        fallos.append(f"{comps}: traceback")
        print(f"  FAIL - {comps}: traceback")
    elif "runtime" not in out:
        fallos.append(f"{comps}: no explica")
        print(f"  FAIL - {comps}: sin mensaje claro")
    else:
        print(f"  ok   - {comps}: error claro sin traceback")

# base sola: ahora es un stack valido
for comps, esperado in ((["psql"], "postgres-only"), (["mysql"], "mysql-only")):
    r = subprocess.run([sys.executable, "-c", DEVOLVER, *comps],
                       capture_output=True, text=True, timeout=120)
    out = (r.stdout + r.stderr).strip().splitlines()
    obtenido = out[-1] if out else ""
    if obtenido != esperado:
        fallos.append(f"{comps}: esperaba {esperado}, salio {obtenido!r}")
        print(f"  FAIL - {comps}: {obtenido!r} != {esperado!r}")
    else:
        print(f"  ok   - {comps} -> {esperado}")

# con runtime sigue andando
for comps, esperado in ((["php", "psql"], "php-postgres"),
                        (["php", "psql", "sftp"], "php-postgres-sftp"),
                        (["nextjs", "python", "psql", "sftp"], "nextjs-python-postgres-sftp")):
    r = subprocess.run([sys.executable, "-c", DEVOLVER, *comps],
                       capture_output=True, text=True, timeout=120)
    out = (r.stdout + r.stderr).strip().splitlines()
    obtenido = out[-1] if out else ""
    if obtenido != esperado:
        fallos.append(f"{comps}: esperaba {esperado}, salio {obtenido!r}")
        print(f"  FAIL - {comps}: {obtenido!r} != {esperado!r}")
    else:
        print(f"  ok   - {comps} -> {esperado}")

print()
print(f"test_db_only_error: {'OK' if not fallos else str(len(fallos)) + ' FALLAS'}")
sys.exit(1 if fallos else 0)
