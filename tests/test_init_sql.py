#!/usr/bin/env python3
"""Los init/ de los stacks tienen que producir SQL valido.

El init se ejecuta UNA SOLA VEZ, cuando el volumen de la base recien se
crea. Si falla ahi, no hay segunda oportunidad: el entrypoint de mariadb
ignora el error, el contenedor arranca igual y el proyecto queda con una
base sin los usuarios del proyecto para siempre, hasta que alguien borre
el volumen a mano.

Durante el desarrollo de appctl casi todos los init estaban rotos y NADA
lo detectaba:

  - 'DB_USER: unbound variable': el compose no pasaba las variables, y como
    la falla daba adentro de la expansion del heredoc, 'set -e' no cortaba.
  - un GRANT con un backslash que MySQL rechazaba con 1064.
  - backticks en COMENTARIOS de un heredoc sin quotear: bash ejecutaba
    'artisan' y metia su ruido adentro del SQL.
  - lineas de mas de 100 chars partidas por el transporte de deploy.

Ninguno se ve leyendo el archivo. Se ven EJECUTANDO el init y mirando que
SQL entrega. Eso es lo que hace este test: se corre el init de verdad con
psql/mariadb substituidos por un binario falso que hace 'cat'. Lo que
llega a la base es exactamente lo que se produce, sin necesidad de una
base de datos.
"""
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
STACKS = RAIZ / "stacks"
VARS = {
    "DB_USER": "app", "DB_PASSWORD": "p1", "DB_NAME": "basedb",
    "DB_MIGRATION_USER": "app_mig", "DB_MIGRATION_PASSWORD": "p2",
    "DB_READONLY_USER": "app_ro", "DB_READONLY_PASSWORD": "p3",
    "DB_ADMIN_USER": "app_admin", "DB_ADMIN_PASSWORD": "p4",
    "MARIADB_ROOT_PASSWORD": "rootpw", "PGPASSWORD": "rootpw",
    "POSTGRES_DB": "basedb", "POSTGRES_USER": "app_admin",
    "MARIADB_DATABASE": "basedb", "MARIADB_USER": "app",
}
FALLOS = []


def check(nombre, cond, detalle=""):
    print(("  PASS  " if cond else "  FAIL  ") + nombre
          + ("" if cond else "\n            {0}".format(detalle)))
    if not cond:
        FALLOS.append(nombre)


def binario_falso(directorio, nombres):
    """Un ejecutable que hace 'cat': imprime lo que le pasan por stdin."""
    for n in nombres:
        f = directorio / n
        f.write_text("#!/bin/bash\ncat\n")
        f.chmod(f.stat().st_mode | stat.S_IEXEC)


def test_init(nombre, rel, debe_aparecer):
    print("=== {0} ===".format(nombre))
    ruta = RAIZ / rel
    check("el archivo existe", ruta.exists(), str(ruta))
    if not ruta.exists():
        return

    lineas = ruta.read_text().split("\n")
    largas = ["L{0} ({1} chars)".format(i + 1, len(l))
              for i, l in enumerate(lineas) if len(l) > 100]
    check("sin lineas de mas de 100 chars (el transporte las parte)",
          not largas, ", ".join(largas[:3]))

    r = subprocess.run(["bash", "-n", str(ruta)], capture_output=True, text=True)
    check("bash -n lo parsea", r.returncode == 0, r.stderr[:200])
    if r.returncode != 0:
        return

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        binario_falso(d, ["psql", "mariadb", "mysql"])
        pgdata = d / "pgdata"
        pgdata.mkdir()
        (pgdata / "pg_hba.conf").write_text("local all all trust\n")
        env = dict(os.environ)
        env["PATH"] = str(d) + os.pathsep + env["PATH"]
        env.update(VARS)
        env["PGDATA"] = str(pgdata)
        try:
            r = subprocess.run(["bash", str(ruta)], capture_output=True, text=True,
                               env=env, timeout=30, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            check("el init no se cuelga", False, "tardo mas de 30s")
            return

    sql = r.stdout
    err = r.stderr or ""
    todo = sql + err

    check("sin 'command not found' (backticks en comentarios)",
          "command not found" not in todo and "orden no encontrada" not in todo,
          [l for l in todo.split("\n")
           if "not found" in l or "orden no encontrada" in l][:2])

    for marcador in debe_aparecer:
        check("el SQL contiene {0!r}".format(marcador),
              marcador in sql, "no aparece en lo que llega a la base")

    faltan = [v for v in ("app", "app_mig", "app_ro") if v not in sql]
    check("los tres usuarios del proyecto estan en el SQL", not faltan, faltan)

    check("sin variables sin expandir", "${" not in sql,
          [l for l in sql.split("\n") if "${" in l][:2])

    sueltas = [l for l in sql.split("\n") if "`" in l]
    check("sin backticks en el SQL entregado", not sueltas, sueltas[:2])

    # El SQL tiene que ser ejecutable de verdad: sin comillas desbalanceadas
    # ni Privilegios que el motor no acepta.
    comillas = sql.count("'") % 2
    check("comillas simples balanceadas", comillas == 0,
          "{0} comillas impares".format(sql.count("'")))

    # MariaDB NO acepta 'GRANT <privilegio> ON <base>.*': el privilegio
    # 'GRANT OPTION' es global y la forma por base se rechaza con 1064.
    # Se chekea aca porque leer el archivo no lo delata: la linea existe y
    # parece razonable.
    if nombre == "mysql":
        # MariaDB no tiene 'GRANT OPTION' como privilegio otorgable: la
        # forma por base se rechaza con 1064 y la global tambien. Para que
        # el usuario de migraciones pueda hacer CREATE USER hacen falta las
        # DOS cosas, verificadas contra mariadb:11.8:
        #   GRANT ALL PRIVILEGES ON <db>.* TO u@'%' WITH GRANT OPTION
        #   GRANT CREATE USER ON *.* TO u@'%'
        #
        # El init estuvo semanas con la forma que no existe y el proyecto
        # quedaba sin los tres usuarios. Leer el archivo no lo delata.
        if "CREATE USER" in sql:
            ok = ("WITH GRANT OPTION" in sql
                  and re.search(r"GRANT CREATE USER ON \*\.\*", sql))
            check("el usuario de migracion puede crear usuarios", ok,
                  "falta WITH GRANT OPTION o el CREATE USER global")
        malos = [l.strip() for l in sql.split("\n")
                 if l.strip().upper().startswith("GRANT OPTION")]
        check("no usa 'GRANT OPTION' (no existe como privilegio)", not malos,
              malos[:2])

def main():
    print("Tests de los init: el SQL que producen tiene que ser valido")
    print("(un init que falla deja la base sin usuarios y no se reintenta)")
    print()
    test_init("mysql", "stacks/php-mysql-sftp/init/10-users.sh",
              ["CREATE USER IF NOT EXISTS", "GRANT", "FLUSH PRIVILEGES"])
    print()
    # postgres arma el SQL con format() y lo ejecuta con \gexec: no hay
    # CREATE ROLE literal.
    test_init("postgres", "stacks/php-postgres-sftp/init/10-roles.sh",
              ["CREATE ROLE %I", "GRANT", "pg_hba"])
    print()
    if FALLOS:
        print("{0} fallo(s): {1}".format(len(FALLOS), ", ".join(FALLOS)))
        return 1
    print("todo bien")
    return 0


if __name__ == "__main__":
    sys.exit(main())
