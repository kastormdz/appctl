#!/usr/bin/env python3
"""Endurecimiento de PHP y nginx en los stacks PHP: gate de configuracion.

QUE VERIFICA: que las directivas de endurecimiento esten en los ARCHIVOS del
stack (php.ini, nginx.conf, entrypoint, compose) y que los dos stacks PHP no
hayan divergido entre si.

QUE NO VERIFICA: que el runtime las aplique. Eso se mira en el contenedor con
`php -i` y `nginx -T`, que es lo que hace la verificacion manual. Un typo en el
NOMBRE de una directiva de php.ini la vuelve un comentario silencioso, y ese
caso SI lo cubre la verificacion con `php -i` en el contenedor; este test no
puede, porque no levanta Docker.

Por que existe: la politica de cookies vive en el php.ini horneado en la
imagen. Si alguien lo reescribe y borra estas lineas, no se nota hasta que el
sitio esta en produccion; y si se toca un solo stack, los dos motores quedan
con politicas distintas sin que nadie lo haya decidido.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PHP_STACKS = ("php-postgres-sftp", "php-mysql-sftp")

# directiva -> valor exigido. El valor es el endurecido por defecto; los que
# se pueden pisar en runtime se listan aparte, porque el default tiene que
# seguir siendo el seguro.
OBLIGATORIAS = {
    "session.use_strict_mode": "1",
    "session.use_only_cookies": "1",
    "session.cookie_httponly": "1",
    "session.cookie_secure": "1",
    "session.cookie_samesite": "Lax",
    "allow_url_fopen": "Off",
    "allow_url_include": "Off",
    "cgi.fix_pathinfo": "0",
    "expose_php": "Off",
}

# se pueden pisar con una env var del proyecto (el entrypoint escribe el
# override). Si alguien las saca de OBLIGATORIAS y no quedan aca, el default
# deja de ser seguro sin que nadie lo vea.
OVERRIDABLES = ("session.cookie_secure", "allow_url_fopen")

NGINX_OBLIGATORIAS = (
    r"autoindex\s+off\s*;",
    r"add_header\s+X-Content-Type-Options\s+nosniff",
)


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  PASS  {msg}")


def ini_valores(texto: str) -> dict[str, str]:
    """Lee las directivas activas de un php.ini (ignora comentarios)."""
    out: dict[str, str] = {}
    for linea in texto.splitlines():
        linea = linea.strip()
        if not linea or linea.startswith(";"):
            continue
        if "=" not in linea:
            continue
        k, v = linea.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def main() -> int:
    print("Endurecimiento de PHP y nginx en los stacks PHP")

    inis: dict[str, dict[str, str]] = {}
    nginx: dict[str, str] = {}
    entrypoints: dict[str, str] = {}
    composes: dict[str, str] = {}

    for stack in PHP_STACKS:
        base = ROOT / "stacks" / stack
        for rel in ("image/php.ini", "image/nginx.conf", "image/entrypoint.sh",
                    "compose.tmpl.yaml"):
            check((base / rel).exists(), f"{stack}: existe {rel}")
        inis[stack] = ini_valores((base / "image/php.ini").read_text())
        nginx[stack] = (base / "image/nginx.conf").read_text()
        entrypoints[stack] = (base / "image/entrypoint.sh").read_text()
        composes[stack] = (base / "compose.tmpl.yaml").read_text()

    # ---- 1. cada directiva endurecida, con su valor ----
    print("\n=== php.ini: directivas endurecidas ===")
    for stack in PHP_STACKS:
        for directiva, esperado in OBLIGATORIAS.items():
            real = inis[stack].get(directiva)
            check(real == esperado,
                  f"{stack}: {directiva} = {esperado}"
                  + ("" if real == esperado else f" (esta en {real!r})"))

    # ---- 2. las que se pueden pisar siguen con default seguro ----
    print("\n=== overrides: el default sigue siendo el seguro ===")
    for stack in PHP_STACKS:
        for directiva in OVERRIDABLES:
            check(inis[stack].get(directiva) in ("1", "Off"),
                  f"{stack}: {directiva} arranca en el valor seguro, no en el "
                  f"permisivo")
    for stack in PHP_STACKS:
        for var in ("APPCTL_SESSION_COOKIE_SECURE", "APPCTL_ALLOW_URL_FOPEN"):
            check(f'[ -n "${{{var}:-}}" ]' in entrypoints[stack],
                  f"{stack}: el entrypoint usa {var} (default vacio = sin "
                  f"override)")
            check(f'case "${{{var}}}" in' in entrypoints[stack],
                  f"{stack}: el entrypoint interpreta {var}")
            check(var in composes[stack],
                  f"{stack}: el compose pasa {var} al contenedor")

    # ---- 3. el override solo aplica cuando la variable esta puesta ----
    print("\n=== override: sin variable no se escribe override ===")
    for stack in PHP_STACKS:
        ep = entrypoints[stack]
        check(re.search(r'\$\{APPCTL_SESSION_COOKIE_SECURE:-\}', ep) is not None,
              f"{stack}: sin APPCTL_SESSION_COOKIE_SECURE no toca el default")
        check("zz-runtime.ini" in ep,
              f"{stack}: escribe el override en zz-runtime.ini")
        # el orden de carga es lo que hace que el override gane
        check("zz-appctl.ini" in ep,
              f"{stack}: nombra zz-appctl.ini (de ahi sale el orden de carga)")
        check('rm -f "${RUNTIME_INI}"' in ep,
              f"{stack}: limpia el override anterior en cada arranque")

    # ---- 4. nginx: listado apagado y cabeceras ----
    print("\n=== nginx: autoindex y cabeceras ===")
    for stack in PHP_STACKS:
        for patron in NGINX_OBLIGATORIAS:
            check(re.search(patron, nginx[stack]) is not None,
                  f"{stack}: nginx tiene {patron}")

    # ---- 5. los dos stacks PHP no divergen ----
    print("\n=== los dos stacks PHP, sin drift ===")
    a, b = PHP_STACKS
    check((ROOT / "stacks" / a / "image" / "php.ini").read_text()
          == (ROOT / "stacks" / b / "image" / "php.ini").read_text(),
          "php.ini identico en los dos stacks PHP")
    check((ROOT / "stacks" / a / "image" / "nginx.conf").read_text()
          == (ROOT / "stacks" / b / "image" / "nginx.conf").read_text(),
          "nginx.conf identico en los dos stacks PHP")


    # ---- las extensiones que el cliente pidio y la imagen NO traia ----
    # Medido en produccion: intl, gd, zip, bcmath y soap no estaban (ni el
    # .so). El comentario del Dockerfile afirmaba que intl venia en la base:
    # falso, y por eso nadie lo noto. Este gate lo mantiene.
    print("\n=== extensiones PHP ===")
    for stack in PHP_STACKS:
        dockerfile = (ROOT / "stacks" / stack / "image" / "Dockerfile").read_text()
        for ext in ("intl", "zip", "gd", "bcmath", "soap"):
            assert "docker-php-ext-install -j1 {0}".format(ext) in dockerfile, \
                "{0}: no instala {1}".format(stack, ext)
        assert "docker-php-ext-configure gd --with-freetype --with-jpeg" in dockerfile, \
            "{0}: gd sin --with-freetype/--with-jpeg".format(stack)
        assert "apk del .ext-build-deps" in dockerfile, \
            "{0}: deja las libs de build en la imagen".format(stack)
        # OJO: anclado al COMANDO, no a la palabra: el comentario del
        # Dockerfile la menciona y el split cortaba ahi.
        primera = dockerfile.split("apk add --no-cache --virtual")[0]
        for lib in ("libpng", "libjpeg-turbo", "freetype"):
            assert re.search(r"(^|\s){0}(\s|\\|$)".format(re.escape(lib)),
                             primera, re.M), \
                "{0}: {1} esta dentro del grupo virtual (apk del se lo lleva)".format(stack, lib)
        assert 'muestra "Zend OPcache" e "intl"' not in dockerfile, \
            "{0}: sigue el comentario que dice que intl viene en la base".format(stack)
    print("\ntodo bien")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\nFALLA: {exc}")
        sys.exit(1)
