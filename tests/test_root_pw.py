#!/usr/bin/env python3
"""El parsing de la password del root de mariadb.

MariaDB 11.x genera la password del root y la imprime en el log del
arranque, cuando el volumen ya estaba inicializado. Esa es la unica que
abre la base, y no esta en ningun archivo.

El log viene con timestamp y nivel adelante, asi que partirse por ':' a lo
bruto devuelve la linea entera (87 chars) y no abre nada. El sintoma es un
'Access denied' que no dice nada, que es exactamente el bug que costo tres
intentos de diagnostico.

Estos tests usan lineas REALES del log, no inventadas.
"""
import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
FALLOS = []

# Lineas reales de docker logs mariadb:11.8, con y sin timestamp.
LINEAS = [
    ("2026-10-03 00:44:25+00:00 [Note] [Entrypoint]: GENERATED ROOT PASSWORD: "
     "F*!2w\"W@|kryiIqK(5#/Nw@+XX|%<wQ3",
     'F*!2w"W@|kryiIqK(5#/Nw@+XX|%<wQ3'),
    ("2026-10-03 00:58:16+00:00 [Note] [Entrypoint]: GENERATED ROOT PASSWORD: "
     "Oe[VvUl$rq3<tiVxmm<d-e!7$t$d|9M7",
     "Oe[VvUl$rq3<tiVxmm<d-e!7$t$d|9M7"),
    ("2026-10-03 00:41:58+00:00 [Note] [Entrypoint]: GENERATED ROOT PASSWORD: "
     ":fu;n{bU:0-D1yQ1l^N].SCz#RD_SeT@",
     ":fu;n{bU:0-D1yQ1l^N].SCz#RD_SeT@"),
]

MARCA = "GENERATED ROOT PASSWORD:"


def check(nombre, cond, detalle=""):
    print(("  PASS  " if cond else "  FAIL  ") + nombre
          + ("" if cond else "\n            {0}".format(detalle)))
    if not cond:
        FALLOS.append(nombre)


def parse(linea):
    """La MISMA logica que lib/smoke.py, importada del codigo real.

    Copiar la funcion aca seria un test que valida una copia: si el codigo
    cambia y el test no, el test sigue verde probando algo que ya no existe.
    """
    import smoke
    return smoke._parse_generated_root_password(linea)


def main():
    print("Parsing de la password del root de mariadb")
    print()
    for linea, esperado in LINEAS:
        got = parse(linea)
        check("extrae la password de una linea de log real",
              got == esperado,
              "obtuvo {0!r} ({1} chars), esperaba {2!r}".format(
                  got[:24], len(got), esperado[:24]))
    print()
    # Una linea que NO es la del root no debe devolver nada.
    otras = ["2026-10-03 00:44:25+00:00 [Note] mysqld: ready for connections",
             "2026-10-03 00:44:25+00:00 [Note] [Entrypoint]: "
             "GENERATED ROOT PASSWORD IS SET: no",
             "2026-10-03 00:44:25+00:00 [Note] [Entrypoint]: "
             "MARIADB_ROOT_PASSWORD: (la variable, no el log)"]
    check("no confunde otras lineas con la password",
          parse(otras[0]) == "",
          "devolvio {0!r}".format(parse(otras[0])))
    # 'IS SET' no lleva password: no hay que devolver 'no'
    check("'IS SET' no se confunde con una password",
          parse(otras[1]) == "",
          "devolvio {0!r}".format(parse(otras[1])))
    print()
    if FALLOS:
        print("{0} fallo(s): {1}".format(len(FALLOS), ", ".join(FALLOS)))
        return 1
    print("todo bien")
    return 0


if __name__ == "__main__":
    sys.exit(main())
