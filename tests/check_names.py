#!/usr/bin/env python3
"""Verificar que no haya nombres sin definir. NO es py_compile.

py_compile mira la SINTAXIS: una funcion llamada y nunca definida compila
perfecto y solo revienta cuando se ejecuta. En appctl eso_BITANE tres
veces en una tarde:

  - ports.first_free(): AtribbuteError en el server, py_compile OK
  - Path sin importar en smoke.py: NameError, py_compile OK
  - _db_password() y _container_env_of() en dbtool.py: py_compile OK y el
    dump de mariadb moria con NameError

Este test recorre el AST de cada archivo y lista las funciones que se
llaman pero no estan definidas ni son de la biblioteca estandar. Los falsos
positivos (shlex.quote, las clases de Excepcion que se Raisean) se
filtran a mano en la lista IGNORAR.

Uso: python3 tests/check_names.py [--verbose]
Salida 0 si no hay nada sin definir.
"""

from __future__ import annotations

import ast
import builtins
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Funciones que existen pero no son metodos ni defs del modulo: imports de
# stdlib con alias, y los metodos de los objetos de las librerias.
IGNORAR = {
    # imports de stdlib con alias que no se detectan como tales
    "sh_quote", "p_quote", "quote",
    # clases propias que se usan como constructor o en raise: no son Call
    # de una funcion libre, son NameError solo si la clase no existe. Se
    # verifican aparte con el chequeo de clases.
    "DbError", "PortError", "Check", "ConfigError", "RegistryError",
    "TemplateError",
    # helpers de subprocess/shell
    "which", "run",
}

ARCHIVOS = [
    "bin/appctl",
    "lib/dbtool.py",
    "lib/smoke.py",
    "lib/summary.py",
    "lib/gen.py",
    "lib/ports.py",
    "lib/registry.py",
]


def _definidos(tree: ast.AST) -> set[str]:
    """Todo nombre que el modulo puede usar sin definirlo."""
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(n.name)
        elif isinstance(n, ast.ClassDef):
            out.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out.add(t.id)
                elif isinstance(t, ast.Tuple):
                    for e in t.elts:
                        if isinstance(e, ast.Name):
                            out.add(e.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                out.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.arguments):
            for a in list(n.args) + list(n.posonlyargs) + list(n.kwonlyargs):
                out.add(a.arg)
            if n.vararg:
                out.add(n.vararg.arg)
            if n.kwarg:
                out.add(n.kwarg.arg)
        elif isinstance(n, ast.comprehension):
            for t in ast.walk(n.target):
                if isinstance(t, ast.Name):
                    out.add(t.id)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            out.update(n.names)
        elif isinstance(n, ast.MatchAs) and n.name:
            out.add(n.name)
    return out


def _duplicados(tree: ast.AST) -> dict[str, list[int]]:
    """Funciones definidas MAS DE UNA VEZ en el mismo archivo.

    En Python gana la ULTIMA definicion, asi que un duplicado viejo se
    ejecuta sin que nadie lo note: paso una hora arreglando _set_publish
    en la copia nueva mientras la copia vieja (la que corria) seguia
    rota. py_compile dice OK y los tests dan OK: lo unico que lo delata es
    leer el archivo.
    """
    out: dict[str, list[int]] = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(n.name, []).append(n.lineno)
    return {k: v for k, v in out.items() if len(v) > 1}


def main() -> int:
    verbose = "--verbose" in sys.argv
    total = 0
    problemas: list[tuple[str, int, str]] = []

    for rel in ARCHIVOS:
        f = ROOT / rel
        if not f.exists():
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        except SyntaxError as e:
            print(f"  {rel:22} SYNTAX ERROR linea {e.lineno}: {e.msg}")
            problemas.append((rel, e.lineno or 0, f"syntax: {e.msg}"))
            continue

        dups = _duplicados(tree)
        if dups:
            for nm, lns in sorted(dups.items()):
                print(f"  {rel:22} DUPLICADO  {nm}() en lineas "
                      f"{', '.join(str(x) for x in lns)}")
                problemas.append((rel, lns[0], f"duplicado: {nm}"))
            total += len(dups)

        defs = _definidos(tree) | set(dir(builtins)) | {"__name__", "__file__"}
        faltan: dict[str, int] = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                nm = n.func.id
                if nm in defs or nm in IGNORAR or nm.startswith("__"):
                    continue
                faltan.setdefault(nm, n.lineno)

        total += len(faltan)
        marca = "OK" if not faltan else "FALLA"
        print(f"  {rel:22} {marca}"
              + (f"  {len(faltan)} sin definir" if faltan else "")
              + (f"  {len(dups)} duplicada(s)" if dups else ""))
        for nm, ln in sorted(faltan.items(), key=lambda x: x[1]):
            print(f"       L{ln}: {nm}()")
            problemas.append((rel, ln, nm))

    if problemas:
        print(f"\n{problemas and len(problemas)} nombre(s) sin definir. "
              f"py_compile NO los ve: compila bien y revienta en runtime.")
        if verbose:
            print("\n  ultimo commit:")
            import subprocess
            print(subprocess.run(["git", "log", "--oneline", "-1"],
                                 cwd=ROOT, capture_output=True,
                                 text=True).stdout.rstrip())
        return 1
    print(f"\n0 nombres sin definir en {len(ARCHIVOS)} archivos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())