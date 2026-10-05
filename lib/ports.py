"""Registro de puertos, con lock de archivo.

Por que el lock: dos `appctl create` en paralelo (dos shells, o un script y
una persona) leen el registry, los dos ven el 8001 libre, los dos lo reservan.
Uno gana, el otro levanta un contenedor que falla con "port is already
allocated", y lo peor es que el registry queda mintiendo sobre who tiene
el 8001.

El lock va en el filesystem, no en memoria, porque los dos procesos son
distintos. `flock(2)` sobre el archivo del lock: el kernel lo libera solo
si el proceso muere, asi que un Ctrl-C no deja el registry trabado.

Por que ademas se chequea `ss -ltnp`: el registry puede mentir. Si alguien
levanto un contenedor a mano, o hay otro servicio del sistema, el puerto esta
ocupado y el registry no lo sabe. Se cree la red, no el archivo.
"""
import fcntl
import json
import os
import re
import socket
import subprocess
from contextlib import contextmanager
from typing import Any

PROJ_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$")

# Puertos que NUNCA se pueden asignar a un proyecto, aunque esten libres.
RESERVED = {
    22, 25, 53, 80, 110, 143, 443, 465, 587, 993, 995, 1433, 3306, 3389,
    5432, 5900, 6379, 8000, 8080, 8443, 9000, 9090, 27017,
}


class PortError(Exception):
    pass


def valid_project(name: str) -> str:
    """Nombre de proyecto: DNS-label. Sin underscore (rompe los labels de
    docker ni los hostnames), sin mayusculas, sin empezar o terminar en guion."""
    if not PROJ_RE.match(name):
        raise PortError(
            f"nombre de proyecto invalido: {name!r}\n"
            "  debe ser 3-32 chars, minusculas, digitos y guiones,\n"
            "  empezando y terminando con letra o digito (ej: acme, app-tic)"
        )
    if name in RESERVED_NAMES:
        raise PortError(f"{name!r} es un nombre reservado de appctl")
    return name


RESERVED_NAMES = {"appctl", "registry", "test", "default", "all"}


def port_free(port: int) -> bool:
    """El puerto esta libre de verdad? pregunta a la red, no al registry."""
    for family, addr in ((socket.AF_INET, ("127.0.0.1", port)),
                         (socket.AF_INET6, ("::1", port))):
        s = socket.socket(family, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(addr)
        except OSError:
            return False
        finally:
            s.close()
    return True


def port_owner(port: int) -> str:
    """Quien tiene el puerto? para el error, que sea util."""
    try:
        out = subprocess.run(
            ["ss", "-ltnp", f"sport = :{port}"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        m = re.search(r'"([^"]+)",pid=(\d+)', out)
        if m:
            return f"{m.group(1)} (pid {m.group(2)})"
        if out.strip().splitlines()[1:]:
            return "otro proceso"
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    return "desconocido"


def _docker_bound_ports() -> set[int]:
    """Puertos publicados por contenedores, leyendo docker."""
    try:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Ports}}"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    ports = set()
    for m in re.finditer(r":(\d+)->", out):
        ports.add(int(m.group(1)))
    return ports


def find_free(start: int, end: int, taken: dict, key: str) -> int:
    """Primer puerto libre en [start, end), honrando lo ya reservado.

    `taken` es el mapa puerto->proyecto del registry (las claves son los
    puertos, los valores los proyectos: NO al revés, por eso se usan las
    claves y no los valores).

    La snapshot de Docker se toma una sola vez: ejecutar `docker ps` por cada
    candidato hacía que un rango ocupado degradara a cientos de subprocesses.
    """
    used = {int(p) for p in taken.keys() if str(p).isdigit()}
    docker_ports = _docker_bound_ports()
    for p in range(start, end):
        if p in RESERVED or p in used or p in docker_ports:
            continue
        if port_free(p):
            return p
    raise PortError(f"no hay puerto libre en [{start},{end}) para {key}")


class Registry:
    """Puerto -> proyecto, y proyecto -> puertos. Con flock."""

    def __init__(self, path: str):
        self.path = path
        self.dir = os.path.dirname(path)
        os.makedirs(self.dir, mode=0o700, exist_ok=True)

    @contextmanager
    def locked(self):
        """Toma el lock de escritura. Todo create/destroy pasa por aca."""
        lock = self.path + ".lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = self._read()
            yield data
            self._write(data)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read(self) -> dict[str, Any]:
        try:
            with open(self.path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"projects": {}, "ports": {}}
        except PermissionError:
            # Un 'sudo appctl create' deja el registro como root:root 0600. Aca
            # reventaba con un PermissionError crudo en medio de un Traceback, que
            # no dice que archivo es ni como se arregla. Lo que importa es que el
            # error sea accionable: el comando de chown va en el mensaje.
            raise PortError(
                f"no puedo leer {self.path}\n"
                "        el archivo es de otro usuario (si se creo con sudo, es\n"
                "        root:root).\n"
                "        sudo chown $(id -un):$(id -gn) " + self.path) from None

    def _write(self, data: dict) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def reserve(self, data: dict, project: str, app_port: int, sftp_port: int) -> None:
        """Guarda las reservas. El caller ya valido que estan libres."""
        data["projects"][project] = {
            "app_port": app_port,
            "sftp_port": sftp_port,
        }
        data["ports"][str(app_port)] = project
        if sftp_port:
            data["ports"][str(sftp_port)] = project

    def check_free(self, data: dict, project: str, app_port: int, sftp_port: int) -> None:
        """Valida contra el registry y contra la red. Falla con mensaje util."""
        if project in data["projects"]:
            raise PortError(f"el proyecto {project!r} ya existe")

        for port in (app_port, sftp_port):
            if not port:
                continue
            owner = data["ports"].get(str(port))
            if owner:
                raise PortError(
                    f"puerto {port} ya asignado al proyecto {owner!r}\n"
                    f"  usalo con --app-port / --sftp-port, o elegi otro"
                )
            if port in RESERVED:
                raise PortError(f"puerto {port} es reservado por el sistema")
            if not port_free(port):
                raise PortError(
                    f"puerto {port} ya OCUPADO en la red por {port_owner(port)}\n"
                    "  el registro no lo sabia: hay un contenedor Docker "
                    "u otro servicio"
                )
            if port in _docker_bound_ports():
                raise PortError(
                    f"puerto {port} ya publicado por un contenedor Docker"
                )

    def release(self, data: dict, project: str) -> None:
        info = data["projects"].pop(project, {})
        for p in (info.get("app_port"), info.get("sftp_port")):
            if p and data["ports"].get(str(p)) == project:
                del data["ports"][str(p)]
