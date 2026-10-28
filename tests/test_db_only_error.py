#!/usr/bin/env python3
"""`appctl proyecto psql` (base sola) tiene que fallar con un mensaje claro.

Sin runtime no hay stack: antes de esto stack_name() explotaba con
TypeError: sequence item 0: expected str instance, NoneType found.
"""
import pathlib
import subprocess
import sys

RAIZ = pathlib.Path(__file__).resolve().parent.parent
CARGAR = f'''
import importlib.machinery, importlib.util, sys, io, contextlib
ARGS = sys.argv[1:]          # los componentes, ANTES de pisar sys.argv
loader = importlib.machinery.SourceFileLoader("appctl", {str(RAIZ / "bin" / "appctl")!r})
spec = importlib.util.spec_from_loader("appctl", loader)
m = importlib.util.module_from_spec(spec)
sys.argv = ["appctl"]
loader.exec_module(m)
buf, err = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
    try:
        m.stack_name(set(ARGS))
    except SystemExit:
        pass
print(err.getvalue() + buf.getvalue())
'''

fallos = []
for comps in (["psql"], ["mysql"], ["psql", "sftp"]):
    r = subprocess.run([sys.executable, "-c", CARGAR, *comps],
                       capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    if "TypeError" in out or "Traceback" in out:
        fallos.append(f"{comps}: explota con un traceback")
        print(f"  FAIL - {comps}: traceback")
        continue
    if "runtime de aplicacion" not in out:
        fallos.append(f"{comps}: no explica que falta el runtime")
        print(f"  FAIL - {comps}: sin mensaje claro")
        continue
    print(f"  ok   - {comps}: error claro sin traceback")

# con runtime sigue andando
for comps, esperado in ((["php", "psql"], "php-postgres"),
                        (["php", "psql", "sftp"], "php-postgres-sftp"),
                        (["nextjs", "python", "psql", "sftp"], "nextjs-python-postgres-sftp")):
    r = subprocess.run([sys.executable, "-c", CARGAR.replace(
        'print(err.getvalue() + buf.getvalue())',
        'print(m.stack_name(set(ARGS)))'), *comps],
        capture_output=True, text=True, timeout=120)
    out = (r.stdout + r.stderr).strip().splitlines()
    obtenido = out[-1] if out else ""
    if obtenido != esperado:
        fallos.append(f"{comps}: esperaba {esperado}, salio {obtenido!r}")
        print(f"  FAIL - {comps}: {obtenido!r} != {esperado!r}")
    else:
        print(f"  ok   - {comps} -> {esperado}")

# y por la puerta real: `appctl <proyecto-que-no-existe> psql`
r = subprocess.run([sys.executable, str(RAIZ / "bin" / "appctl"),
                    "un-proyecto-que-no-existe-xyz", "psql"],
                   capture_output=True, text=True,
                   env=dict(__import__("os").environ, PYTHONPATH=str(RAIZ / "lib")),
                   timeout=120)
out = r.stdout + r.stderr
if "Traceback" in out:
    fallos.append("appctl <proj> psql: traceback")
    print("  FAIL - appctl <proj> psql: traceback")
elif "no existe el proyecto" in out and "no es un stack" in out:
    print("  ok   - appctl <proj> psql: dice que el proyecto no existe y que falta el runtime")
else:
    fallos.append("appctl <proj> psql: mensaje confuso")
    print("  FAIL - appctl <proj> psql: " + out.strip().splitlines()[-1][:90])

print()
print(f"test_db_only_error: {'OK' if not fallos else str(len(fallos)) + ' FALLAS'}")
sys.exit(1 if fallos else 0)
