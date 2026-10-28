#!/usr/bin/env python3
"""`appctl -h` sin parametros tiene que informar la VERSION de cada componente.

Antes las bases salian sin version ("psql PostgreSQL", "mysql MariaDB") y no se
sabia que se estaba ofreciendo. Con el texto viejo este test falla.
"""
import os
import pathlib
import re
import subprocess
import sys

RAIZ = pathlib.Path(__file__).resolve().parent.parent
fallos = []


def check(ok, msg):
    if not ok:
        fallos.append(msg)
        print(f"  FAIL - {msg}")
    else:
        print(f"  ok   - {msg}")


env = dict(os.environ, PYTHONPATH=str(RAIZ / "lib"))
r = subprocess.run([sys.executable, str(RAIZ / "bin" / "appctl"), "-h"],
                   capture_output=True, text=True, env=env, timeout=120)
out = r.stdout + r.stderr
check(r.returncode == 0, "appctl -h corre")
check("componentes:" in out, "trae la seccion de componentes")

# cada linea de componente con version donde corresponde
lineas = {l.split()[0].strip(): l for l in out.splitlines()
          if l.startswith("  ") and l.strip() and not l.startswith("   ")}

check(re.search(r"psql\s+PostgreSQL \d+", out) is not None,
      "psql informa la version de PostgreSQL")
check(re.search(r"mysql\s+MariaDB \d+\.\d+|\d+", out) is not None,
      "mysql informa la version de MariaDB")
check(re.search(r"php\s+PHP-FPM \d+\.\d+", out) is not None,
      "php informa la version de PHP")
check(re.search(r"tomcat\s+Tomcat \d+", out) is not None,
      "tomcat informa la version")
check(re.search(r"nextjs\s+Node \d+", out) is not None,
      "nextjs informa la version de Node")
check("v2.4.1" in out or re.search(r"Garage v?\d+\.\d+", out) is not None,
      "s3 informa la version de Garage")
check("8.5-fpm-alpine" not in out,
      "php no muestra el tag crudo (8.5-fpm-alpine): se muestra la version")

print()
print(f"test_help_versiones: {'OK' if not fallos else str(len(fallos)) + ' FALLAS'}")
sys.exit(1 if fallos else 0)
