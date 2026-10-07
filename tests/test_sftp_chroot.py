#!/usr/bin/env python3
"""Chroot minimalista del SFTP: gate de TODOS los stacks.

QUE VERIFICA: en los ARCHIVOS de cada stack (entrypoint, sshd_config) que el
chroot que ve el cliente trae solo /upload y /private, nada mas: ni
andamiaje de binarios, ni /dev (medido: internal-sftp en proceso anda sin
/dev/null), ni /tmp (no se crea mas; en proyectos viejos se quita solo si
esta vacio, con rmdir).

Por que cubre TODOS los stacks y no solo los PHP: el andamiaje se saco
primero de los PHP y las copias se quedaron atras. El stack de Next y el de
Tomcat seguian copiando sftp-server, libs, shells y device nodes, con el
cliente viendo bin/dev/etc/lib/proc/usr al entrar por SFTP. Un gate que
mira un solo runtime no ve el drift entre runtimes: este itera los stacks
del disco, no una lista escrita a mano.

QUE NO VERIFICA: el listado real por SFTP ni que el login siga andando
despues del cambio. Eso se prueba con login SFTP contra un contenedor
efimero, como manda la regla: lo que pasa adentro del contenedor no lo
prueba un grep.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Andamiaje que no puede existir mas en el chroot: ni creado ni limpiado con
# comodines. upload/private son del cliente y no se tocan jamas. tmp no se
# crea; lib64 solo lo tiene el stack Debian (tomcat), y se acepta.
ANDAMIAJE = {"bin", "usr", "lib", "lib64", "etc", "proc", "dev"}
CLIENTE = {"upload", "private", "tmp"}


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  PASS  {msg}")


def stacks_con_sftp() -> list[str]:
    """Todo stack que tenga entrypoint: si tiene SFTP, ahi esta el chroot."""
    out = []
    for d in sorted((ROOT / "stacks").iterdir()):
        if d.is_dir() and (d / "image" / "entrypoint.sh").is_file():
            out.append(d.name)
    if not out:
        raise AssertionError("no encontre ningun stack con entrypoint")
    return out


def main() -> int:
    print("Chroot minimalista del SFTP (todos los stacks)")
    stacks = stacks_con_sftp()
    print(f"  stacks: {', '.join(stacks)}\n")
    entrypoints = {s: (ROOT / "stacks" / s / "image" / "entrypoint.sh").read_text()
                   for s in stacks}
    sshd = {}
    for s in stacks:
        f = ROOT / "stacks" / s / "image" / "sshd_config"
        sshd[s] = f.read_text() if f.is_file() else ""

    # ---- 1. subsystem en proceso, nada externo ----
    print("=== sshd_config: SFTP en proceso ===")
    for s in stacks:
        ep = entrypoints[s]
        # el stack de tomcat escribe su sshd_config en el entrypoint (Debian,
        # sin archivo aparte); los demas lo traen como archivo.
        conf = sshd[s] or ep
        # Los COMENTARIOS tienen que poder explicar de que se saco el
        # andamiaje sin que el gate los lea como configuracion: se miran las
        # lineas que sshd lee de verdad.
        conf_codigo = "\n".join(l for l in conf.splitlines()
                                if not l.strip().startswith("#"))
        check("Subsystem sftp internal-sftp" in conf_codigo,
              f"{s}: el subsystem no es internal-sftp")
        check("sftp-server" not in conf_codigo.replace("internal-sftp", ""),
              f"{s}: sshd_config todavia nombra el binario externo")

    # ---- 2. el entrypoint no mete binarios al chroot ----
    print("\n=== entrypoint: sin andamiaje ===")
    for s in stacks:
        ep = entrypoints[s]
        check("SFTP_BIN" not in ep,
              f"{s}: todavia resuelve el binario sftp-server")
        for d in ("usr", "lib", "bin", "dev"):
            check(f"SFTP_CHROOT}}/{d}\"" not in ep,
                  f"{s}: todavia crea {d}/ en el chroot")
        check('SFTP_CHROOT}/etc"' not in ep,
              f"{s}: todavia crea etc/ en el chroot")
        check("ldd " not in ep,
              f"{s}: todavia caza .so para el chroot")
        check("/usr/lib/ssh/sftp-server" not in ep
              and "/usr/lib/openssh/sftp-server" not in ep,
              f"{s}: todavia usa el binario externo en el chroot")
        check('chroot "${SFTP_CHROOT}"' not in ep,
              f"{s}: todavia prueba binarios con chroot")

    # ---- 3. limpieza de proyectos viejos, explicita y acotada ----
    print("\n=== limpieza: lista cerrada, sin comodines ===")
    for s in stacks:
        ep = entrypoints[s]
        m = re.search(r"for _rm in ([a-z0-9 ]+); do", ep)
        if m is None:
            raise AssertionError(
                f"{s}: no hay limpieza del andamiaje viejo en el entrypoint")
        lista = set(m.group(1).split())
        check(lista <= ANDAMIAJE,
              f"{s}: la lista de borrado tiene algo que no es andamiaje: "
              f"{sorted(lista - ANDAMIAJE)}")
        check(not (lista & CLIENTE),
              f"{s}: {sorted(lista & CLIENTE)} del cliente cayo en la lista "
              f"de borrado")
        check("${SFTP_CHROOT:?}" in ep,
              f"{s}: el rm de limpieza va sin guardia :?")
        check('rm -rf "${SFTP_CHROOT:?}/${_rm}"' in ep,
              f"{s}: el rm de limpieza no esta acotado a la variable _rm")
        check('mkdir -p "${SFTP_CHROOT}/dev"' not in ep,
              f"{s}: todavia crea /dev (medido que no hace falta)")
        check('SFTP_CHROOT}/tmp"' not in ep.replace('rmdir "${SFTP_CHROOT}/tmp"', ""),
              f"{s}: todavia crea /tmp en el chroot (es pasajero, fuera)")
        check('rmdir "${SFTP_CHROOT}/tmp"' in ep,
              f"{s}: no quita el /tmp viejo vacio (solo rmdir, nunca rm -rf)")
        check('SFTP_CHROOT}/proc' not in ep,
              f"{s}: todavia crea proc/ en el chroot")

    # ---- 4. lo que sostiene el login sigue intacto ----
    print("\n=== el login no se rompe ===")
    for s in stacks:
        ep = entrypoints[s]
        check("ForceCommand internal-sftp -d /upload" in ep,
              f"{s}: falta el ForceCommand que para al cliente en /upload")
        check("mknod" not in ep.replace("# ", ""),
              f"{s}: todavia crea device nodes (medidos innecesarios)")
        check('rm -rf "${SFTP_CHROOT}/upload"' not in ep,
              f"{s}: volvio el rm -rf sobre upload")
        check("PRIVATEDIR" in ep,
              f"{s}: no crea el directorio privado del cliente")

    # ---- 5. los sshd_config de archivo, sin drift ----
    print("\n=== sshd_config: sin drift entre stacks con archivo ===")
    con_archivo = {s: sshd[s] for s in stacks if sshd[s]}
    if con_archivo:
        base = sorted(con_archivo)[0]
        for s, conf in con_archivo.items():
            check(conf == con_archivo[base],
                  f"{s}: sshd_config difiere de {base}")

    print("\ntodo bien")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\nFALLA: {exc}")
        sys.exit(1)

