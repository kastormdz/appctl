#!/usr/bin/env python3
"""Cada claim del README tiene que existir de verdad.

Un README que afirma sin verificar es peor que uno incompleto: el lector no
tiene forma de saber que miente. Y es lo que pasó con este repo:

  - decia 'Hay 8 stacks' y hay 4
  - listaba lib/compose.py, lib/secrets.py y lib/db.py que no existen
  - decia php-postgres-cron y nextjs-mysql-sftp, stacks que no estan built
  - el ejemplo del smoke test usaba postgres:16 con stacks en 18
  - y un compose.yaml de ejemplo escrito de memoria, que no reflejaba el
    compose.tmpl.yaml real

Los primeros cuatro venían del diseño original: se planificó así, la
implementación consolidó, y el README nunca se reconcilió con el código. Eso
también es inventar.

Este test extrae las afirmaciones verificables del README y las contrasta
contra los archivos. Lo que no se puede verificar mecánicamente tiene que
decirlo el propio README, o no está.
"""
import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
README = RAIZ / "README.md"
FALLOS = []


def check(nombre, cond, detalle=""):
    print(("  PASS  " if cond else "  FAIL  ") + nombre
          + ("" if cond else "\n            {0}".format(detalle)))
    if not cond:
        FALLOS.append(nombre)


def leer(rel):
    p = RAIZ / rel
    return p.read_text() if p.is_file() else None


def todo_el_codigo():
    """Todo el contenido del repo, para buscar flags y tags."""
    partes = []
    for f in RAIZ.rglob("*"):
        if not f.is_file() or "/.git/" in str(f):
            continue
        if f.name == Path(__file__).name:
            continue
        try:
            partes.append(f.read_text(errors="replace"))
        except Exception:
            pass
    return "\n".join(partes)


def main():
    rc = 0
    for nombre, comillas in (("README.md", "«"), ("README.en.md", "'")):
        print("=" * 62)
        print("{0}  (el principal es el español)".format(nombre))
        print("=" * 62)
        if not (RAIZ / nombre).is_file():
            check("el {0} existe".format(nombre), False, "no esta")
            rc = 1
            continue
        if main_una(nombre) != 0:
            rc = 1
        print()
    if FALLOS:
        print("{0} fallo(s) en total".format(len(FALLOS)))
        for f in FALLOS:
            print("  - {0}".format(f))
        return 1
    print("todo bien")
    return rc


def main_una(nombre):
    global README
    README = RAIZ / nombre
    t = README.read_text()
    app = leer("bin/appctl") or ""
    codigo = todo_el_codigo()

    # ---- 1. los stacks que el README nombra ----
    print("=== stacks ===")
    reales = sorted(d.name for d in (RAIZ / "stacks").iterdir() if d.is_dir())
    # El nombre del stack puede tener mas de un runtime adentro
    # (nextjs-python-postgres-sftp): el patron acepta segmentos de mas, o el
    # stack de dos runtimes pasaba sin que nadie verificara que existe.
    dichos = set(re.findall(
        r"`([a-z]+(?:-[a-z]+)*-(?:postgres|mysql)-(?:sftp|cron))`", t))
    for s in sorted(dichos):
        check("el stack {0} existe".format(s), s in reales,
              "el README lo nombra y stacks/ no lo tiene")
    m = re.search(r"Hay \*\*(\d+)\*\*", t)
    if m:
        check("el conteo de stacks coincide con stacks/",
              int(m.group(1)) == len(reales),
              "el README dice {0}, hay {1}".format(m.group(1), len(reales)))
    print("  reales: {0}".format(", ".join(reales)))
    print()

    # ---- 2. los archivos y directorios que el README nombra ----
    print("=== archivos ===")
    for rel in sorted(set(re.findall(r"`((?:lib|bin|tests|stacks|docs)/[\w.*/-]+)`", t))):
        if "*" in rel:
            check("el glob {0} matchea".format(rel), bool(list(RAIZ.glob(rel))),
                  "no matchea nada")
        else:
            check("existe {0}".format(rel), (RAIZ / rel).exists(),
                  "el README lo nombra y no esta")
    for nombre in sorted(set(re.findall(r"([a-z_]+\.py)\s+#", t))):
        ok = any((RAIZ / d / nombre).exists() for d in ("lib", "tests", "bin"))
        check("existe {0}".format(nombre), ok,
              "el README lo nombra y no esta en lib/, tests/ ni bin/")
    for nombre in sorted(set(re.findall(r"^\s*[│├└─\s]*([a-z-]+)/\s+#", t, re.M))):
        # el README puede listar directorios que NO van en el repo publico
        # (docs/ es privado). Eso es correcto si lo dice explicitamente.
        priv = re.search(r"^\s*[│├└─\s]*{0}/.*\((privado|private)".format(
            re.escape(nombre)), t, re.M | re.I)
        if (RAIZ / nombre).is_dir() or priv:
            continue
        check("existe el directorio {0}/".format(nombre), False,
              "el README lo nombra, no esta, y no dice que es privado")
    print()

    # ---- 3. los flags que el README documenta ----
    print("=== flags ===")
    for flag in sorted(set(re.findall(r"`(--[a-z-]+)", t))):
        check("el flag {0} existe en el codigo".format(flag),
              '"{0}"'.format(flag) in codigo,
              "el README lo documenta y no aparece en ningun archivo")
    print()

    # ---- 4. los subcomandos ----
    print("=== comandos ===")
    subs = set(re.findall(r'add_parser\(\s*"([a-z-]+)"', app))
    for c in sorted(set(re.findall(r"appctl <p(?:royecto)?>\s+db\s+([a-z-]+)", t))):
        check("el subcomando 'db {0}' existe".format(c), c in subs,
              "el README lo documenta y el parser no lo tiene")
    print()

    # ---- 5. las versiones por defecto ----
    print("=== versiones por defecto ===")
    for var, pat in [("APPCTL_PHP_TAG", r'APPCTL_PHP_TAG",\s*"([\w.\-]+)'),
                     ("APPCTL_PG_TAG", r'APPCTL_PG_TAG",\s*"([\w.\-]+)'),
                     ("APPCTL_MYSQL_TAG", r'APPCTL_MYSQL_TAG",\s*"([\w.\-]+)')]:
        m = re.search(pat, app)
        if m:
            check("{0}={1} aparece en el README".format(var, m.group(1)),
                  m.group(1) in t,
                  "el codigo lo usa y el README no lo menciona")
    print()

    # ---- 6. los paths por defecto ----
    print("=== paths por defecto ===")
    for var in ("APPCTL_PROJECTS", "APPCTL_HOME", "APPCTL_HOST"):
        m = re.search(var + r'",\s*"([^"]+)"', app)
        if m:
            check("el default de {0} esta en el README".format(var),
                  m.group(1) in t,
                  "el codigo usa '{0}' y el README no lo dice".format(m.group(1)))
    m = re.search(r'add_argument\("--cidr",\s*default="([^"]+)"', app)
    if m:
        check("el default de --cidr esta en el README", m.group(1) in t,
              "el codigo usa '{0}' y el README no lo dice".format(m.group(1)))
    print()

    # ---- 7. las redes de cada stack ----
    print("=== redes ===")
    for stack in reales:
        tmpl = leer("stacks/{0}/compose.tmpl.yaml".format(stack))
        if not tmpl:
            continue
        for r in set(re.findall(r"^  ([a-z_]+):\n    driver: bridge", tmpl, re.M)):
            check("la red '{0}' de {1} esta en el README".format(r, stack),
                  r in t, "el README no la menciona")
    print()

    # ---- 8. los tags de imagen ----
    print("=== tags por defecto ===")
    # Solo los tags BASE de un stack: lo que va en el compose del proyecto.
    # Las imagenes auxiliares (busybox para los checks de red, por ejemplo)
    # no aparecen en el README y no deberian: son internas del chequeo.
    base_tags = set()
    for f in (RAIZ / "stacks").rglob("*"):
        if f.is_file() and f.suffix in (".yaml", ".yml"):
            for tag in re.findall(r"image:\s*[\"']?([a-z]+:[\w.\-]+)",
                                  f.read_text(errors="replace")):
                # __PLACEHOLDER__ es lo que el CLI reemplaza en create; no
                # es un tag concreto y no puede estar en el README
                if "__" not in tag:
                    base_tags.add(tag)
    for tag in sorted(base_tags):
        check("el tag base {0} esta en el README".format(tag), tag in t,
              "es el tag del compose del stack y el README no lo menciona")
    # ---- 9. la ayuda del CLI nombra todo lo que existe ----
    # `-h` es una superficie de documentacion mas: el resumen de `db` nombraba
    # 4 de los 9 subcomandos, y nadie lo notaba porque el listado de argparse
    # (que si esta completo) se imprime debajo. Se compara el resumen contra
    # los subparsers REGISTRADOS en el codigo.
    print("=== la ayuda del CLI vs lo que existe ===")
    cli = (RAIZ / "bin" / "appctl").read_text(errors="replace")
    # El resumen puede estar partido en varias lineas (literales adyacentes):
    # hay que juntarlos, o el gate solo ve el primer fragmento.
    # Los literales adyacentes pueden traer parentesis adentro ("users)"), y
    # cortar en el primer ')' perdia el ultimo fragmento. Se pide: uno o mas
    # literales seguidos del cierre de la llamada.
    m = re.search(r'add_parser\("db", help=((?:"[^"]*"\s*)+)\)', cli)
    resumen = " ".join(re.findall(r'"([^"]*)"', m.group(1))) if m else ""
    check("el parser db declara su resumen", bool(m))
    if m:
        subs = sorted(set(re.findall(r'dsub\.add_parser\("([a-z]+)"', cli)))
        check("el parser db tiene subcomandos", len(subs) >= 8)
        for nombre in subs:
            check("la ayuda de `db` nombra {0}".format(nombre), nombre in resumen,
                  "el subcomando existe y el resumen de -h no lo dice")
    # expose: el flag y su default tienen que estar en la ayuda
    check("expose declara --bind con default 127.0.0.1",
          'add_argument("--bind", default="127.0.0.1"' in cli)
    check("expose explica que persiste en state.json",
          "ANOTADOS en state.json" in cli)
    check("el epilogo tiene un ejemplo de expose", "db expose 5432" in cli)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())