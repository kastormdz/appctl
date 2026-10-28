#!/usr/bin/env python3
"""Allow-list de origen en pg_hba: `db grant usuario 192.168.33.66`.

El bug que cierra: la linea del grant se agregaba al FINAL de pg_hba, despues
de `host all all 172.16.0.0/12` y `192.168.0.0/16`; en pg_hba gana la primera
que coincide, asi que el origen del grant nunca se evaluaba.
"""
import pathlib
import sys

RAIZ = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "lib"))
import dbtool  # noqa: E402

fallos = []


def check(ok, msg, detalle=""):
    if not ok:
        fallos.append(msg)
        print(f"  FAIL - {msg}" + (f"  [{detalle}]" if detalle else ""))
    else:
        print(f"  ok   - {msg}")


print("=== normalizacion del origen")
casos = [
    ("192.168.33.66", "php-postgres-sftp", "192.168.33.66"),
    ("192.168.0.0/24", "php-postgres-sftp", "192.168.0.0/24"),
    ("  10.20.0.0/16 ", "php-postgres-sftp", "10.20.0.0/16"),
    ("192.168.33.66", "php-mysql-sftp", "192.168.33.66"),
    ("192.168.0.0/24", "php-mysql-sftp", "192.168.0.%"),
]
for entrada, stack, esperado in casos:
    try:
        obtenido = dbtool.normalize_origen(entrada, stack)
    except Exception as e:  # noqa: BLE001
        obtenido = f"ERROR {e}"
    check(obtenido == esperado, f"{entrada!r} en {stack} -> {esperado}", obtenido)

for malo in ("10.0.0.5/8", "no-es-una-ip", "192.168.0.0/33"):
    try:
        dbtool.normalize_origen(malo, "php-postgres-sftp")
        check(False, f"{malo!r} tiene que rechazarse", "lo acepto")
    except Exception:
        check(True, f"{malo!r} se rechaza")

print("\n=== el bloque de pg_hba")
b = dbtool.hba_bloque("midb_db", "172.20.0.0/16",
                      [{"user": "cliente", "cidr": "192.168.33.66"},
                       {"user": "rep", "cidr": "192.168.0.0/24"}])
lineas = b.splitlines()
check(lineas[0] == dbtool.HBA_BEGIN, "arranca con el marcador (gana la primera regla)")
check(b.rstrip().endswith(dbtool.HBA_END), "cierra con el marcador")
check("host    all   all   127.0.0.1/32" in b, "permite localhost")
check("172.20.0.0/16" in b, "permite la red del proyecto (el contenedor de la app)")
check("192.168.33.66" in b and "192.168.0.0/24" in b, "lista los origenes del grant")
check("host    all   all   all                    reject" in b, "termina en reject")
i_ultimo_allow = max(b.index("192.168.33.66"), b.index("192.168.0.0/24"))
check(i_ultimo_allow < b.index("reject"), "el reject va DESPUES de las permitidas")
check("172.16.0.0/12" not in b and "192.168.0.0/16  " not in b,
      "no re-abre las redes anchas viejas (172.16/12, 192.168/16)")
check(b.count("reject") == 1, "un solo reject")

print("\n=== sin origenes externos (proyecto nuevo)")
b0 = dbtool.hba_bloque("midb_db", "172.20.0.0/16", [])
check("reject" in b0, "igual cierra todo lo externo")
check(b0.count("host    midb_db") == 0, "no inventa lineas de usuario")

print("\n=== el CLI lo usa")
cli = (RAIZ / "bin" / "appctl").read_text(encoding="utf-8")
check('q.add_argument("origen", nargs="?"' in cli,
      "`db grant usuario <origen>` acepta el origen posicional")
check("db.normalize_origen(origen, stack)" in cli, "normaliza el origen")
check('st["db_access"] = acc' in cli, "persiste la allow-list en state.json")
check("db.hba_aplicar(d, stack, acc)" in cli, "aplica la allow-list al dar/quitar acceso")
check("_dbt.hba_aplicar(pdir, stack, [])" in cli,
      "un proyecto nuevo arranca con la allow-list cerrada")

print()
print(f"test_db_hba: {'OK' if not fallos else str(len(fallos)) + ' FALLAS'}")
sys.exit(1 if fallos else 0)
