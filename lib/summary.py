"""El output que va a leer el developer.

Criterio: el developer tiene que poder copiar-pegar todo lo que necesita sin
preguntarte nada, y vos no deberias tener que editar el texto antes de
mandarlo. Por eso el resumen va en texto plano, con los labels alineados y
sin color (o con poco: que se lea bien pegado en un mail o en un ticket).
"""
import os
import json
import subprocess
from pathlib import Path

BOLD = "\033[1m"
DIM = "\033[2m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def _plain(s: str) -> str:
    return s


# Credenciales que NUNCA van al summary: son de administracion del
# contenedor, no del cliente.
SECRET_ONLY = {"DB_ROOT_PASSWORD"}


def addr_publicada(bind: str, puerto: str, host: str) -> str:
    """La direccion que le sirve a un cliente, a partir del bind real.

    0.0.0.0 quiere decir "todas las interfaces": lo util es el nombre del host,
    porque nadie se conecta a 0.0.0.0. Al reves, 127.0.0.1 es "solo este host":
    ahi el nombre del host seria una mentira, porque de afuera no entra.
    """
    if bind in ("", "0.0.0.0", "::", "[::]"):
        return f"{host}:{puerto}"
    return f"{bind}:{puerto}"


def alcance_bind(bind: str) -> str:
    """El sufijo que dice el alcance. Sin esto, 'publicada en' no dice nada."""
    if _es_local(bind):
        return "  (solo este host: de afuera no entra)"
    if bind in ("", "0.0.0.0", "::", "[::]"):
        return "  (abierta a cualquier red que llegue)"
    return ""


def _es_local(bind: str) -> bool:
    return bind in ("127.0.0.1", "::1", "[::1]")


def render_create(project: str, stack: str, host: str, app_port: int,
                  sftp_port: int | None, names: dict, pw: dict,
                  pdir: str, creds_only: bool = False,
                  db_publish: str | None = None) -> str:
    pw = {k: v for k, v in pw.items() if k not in SECRET_ONLY}
    L = []
    bar = "-" * 62
    L.append("")
    L.append(f"{BOLD}{project}{RESET}  {DIM}({stack}){RESET}")
    L.append(f"{bar}")

    L.append(f"{BOLD}Acceso a la aplicacion{RESET}")
    L.append(f"  URL        http://{host}:{app_port}")
    # el codigo vive en sftp/home/upload: es el MISMO directorio que ve el
    # cliente por SFTP y que sirve nginx. Decir "app/" hace pensar que hay
    # dos lugares, y no los hay.
    L.append(f"  Codigo     {pdir}/sftp/home/upload   (sube por SFTP)")
    L.append("")

    # nextjs-python: el stack corre DOS runtimes. El developer tiene que
    # saber por donde entra cada uno sin preguntar nada.
    if "python" in stack:
        L.append(f"{BOLD}API Python{RESET}  {DIM}(FastAPI + uvicorn, detras de nginx){RESET}")
        L.append(f"  URL        http://{host}:{app_port}/api/")
        L.append(f"  Codigo     {pdir}/sftp/home/upload/backend   (sube por SFTP)")
        L.append(f"  {DIM}Se arranca con uvicorn: main:app desde ese directorio{RESET}")
        L.append(f"  {DIM}(o app.main:app si usas un paquete app/). El prefijo{RESET}")
        L.append(f"  {DIM}/api se SACA al reenviar: tu /items se ve en /api/items.{RESET}")
        L.append(f"  {DIM}Las deps van en backend/requirements.txt y se instalan{RESET}")
        L.append(f"  {DIM}al arrancar el contenedor. Tu API tiene que responder{RESET}")
        L.append(f"  {DIM}GET /healthz (200) o el contenedor figura unhealthy.{RESET}")
        L.append("")

    # Tomcat: el manager usa la MISMA credencial que el SFTP.
    if stack.startswith("tomcat"):
        L.append(f"{BOLD}Tomcat manager{RESET}  {DIM}(misma credencial que el SFTP){RESET}")
        L.append(f"  URL        http://{host}:{app_port}/manager/html")
        L.append(f"  User       {names['SFTP_USER']}")
        L.append(f"  Pass       {pw['SFTP_PASSWORD']}")
        L.append(f"  {DIM}(deploy de .war por HTTP con el rol 'tomcat'){RESET}")
        L.append("")

    if sftp_port:
        L.append(f"{BOLD}Acceso por SFTP{RESET}")
        L.append(f"  Host       {host}")
        L.append(f"  Puerto     {sftp_port}")
        L.append(f"  Usuario    {names['SFTP_USER']}")
        L.append(f"  Password   {pw['SFTP_PASSWORD']}")
        L.append(f"  Carpeta    /upload/            (dentro del chroot)")
        L.append(f"  Comando    sftp -P {sftp_port} {names['SFTP_USER']}@{host}")
        # Tip real, verificado: una maquina con varias llaves en ~/.ssh
        # ofrece las 5 (id_rsa, id_ecdsa, id_ecdsa_sk, id_ed25519,
        # id_ed25519_sk) ANTES de que le pidan la password, y el server
        # corta antes. Sin esto el developer ve "Too many authentication
        # failures" y piensa que la password esta mala.
        L.append(f"  {DIM}Si dice 'Too many authentication failures',{RESET}")
        L.append(f"  {DIM}esa maquina prueba varias llaves antes de la password. Usar:{RESET}")
        L.append(f"  {DIM}  sftp -o PreferredAuthentications=password -P {sftp_port} "
                 f"{names['SFTP_USER']}@{host}{RESET}")
        L.append("")

    # el puerto y el driver los define el stack. Con 5432/pgsql hardcodeados,
    # un proyecto MariaDB receive datos que lo rompen (pg_isready contra un
    # MariaDB, DB_CONNECTION=pgsql en un proyecto MySQL).
    db_port = "3306" if "mysql" in stack else "5432"
    db_driver = "mysql" if "mysql" in stack else "pgsql"
    if "mysql" in stack:
        db_driver = "mysql"

    L.append(f"{BOLD}Base de datos{RESET}  {DIM}(solo accesible desde la app){RESET}")
    L.append(f"  Servidor   {host}:{db_port}  {DIM}(NO abrir desde afuera){RESET}")
    L.append(f"  Base       {names['DB_NAME']}")
    L.append("")
    L.append(f"  {BOLD}Usuario de la aplicacion{RESET}")
    L.append(f"    User     {names['DB_USER']}")
    L.append(f"    Pass     {pw['DB_PASSWORD']}")
    L.append(f"    Permisos SELECT / INSERT / UPDATE / DELETE / TEMP TABLE")
    L.append(f"    {DIM}(sin CREATE ni DROP de tablas permanentes: no "
                 f"puede borrar el schema){RESET}")
    L.append("")
    L.append(f"  {BOLD}Usuario de migraciones{RESET}  {DIM}(para artisan migrate, etc){RESET}")
    L.append(f"    User     {names['DB_MIGRATION_USER']}")
    L.append(f"    Pass     {pw['DB_MIGRATION_PASSWORD']}")
    L.append(f"    Permisos ALL sobre la base")
    L.append("")
    L.append(f"  {BOLD}Usuario de solo lectura{RESET}  {DIM}(reportes, backups){RESET}")
    L.append(f"    User     {names['DB_READONLY_USER']}")
    L.append(f"    Pass     {pw['DB_READONLY_PASSWORD']}")
    L.append(f"    Permisos SELECT")
    L.append("")

    if stack.startswith("tomcat"):
        L.append(f"{BOLD}Credenciales Java{RESET}  {DIM}dentro del contenedor{RESET}")
        L.append(f"  properties  /usr/local/tomcat/conf/appctl-db.properties")
        L.append(f"               (tambien en el classpath, 0600)")
        L.append(f"  JNDI        /usr/local/tomcat/conf/appctl-context-template.xml")
        L.append(f"               (copiala a META-INF/context.xml en tu .war)")
        L.append(f"  driver      org.postgresql.Driver  (lo pone el pom/build.gradle)")
        L.append("")

    # La base publicada es lo PRIMERO que tiene que saber quien recibe esto:
    # la frase "no se abre desde internet" estaba hardcodeada y con un puerto
    # expuesto era falsa (medido: el admin expuso 5435 y el resumen no lo decia).
    if db_publish:
        _pb, _, _pp = db_publish.rpartition(":")
        L.append(f"{BOLD}{YELLOW}Base publicada{RESET}")
        L.append(f"  {YELLOW}publicada en {addr_publicada(_pb, _pp, host)}{RESET}"
                 f"{YELLOW}{alcance_bind(_pb)}{RESET}")
        if _es_local(_pb):
            L.append(f"  {DIM}De afuera NO entra. Si el cliente se conecta desde")
            L.append(f"  otra maquina, hay que re-exponerla:")
            L.append(f"    appctl {project} db expose {_pp} --bind 0.0.0.0{RESET}")
        else:
            L.append(f"  {DIM}Entra quien alcance esa direccion. Se cierra con:")
            L.append(f"    appctl {project} db unexpose{RESET}")
        L.append("")

    L.append(f"{BOLD}Ejemplo de conexion (Laravel .env){RESET}")
    if not db_publish:
        L.append(f"  {DIM}La base no esta publicada (red interna). Esta es la config")
        L.append(f"  para la app DENTRO del contenedor (host 'db'). Para un cliente")
        L.append(f"  externo hace falta un tunel: ssh -L {int(db_port)+100}:db:{db_port} <usuario>@<host>{RESET}")
    else:
        L.append(f"  {DIM}Dentro del contenedor:{RESET}")
    L.append(f"  DB_CONNECTION={db_driver}")
    L.append(f"  DB_HOST=db")
    L.append(f"  DB_PORT={db_port}")
    L.append(f"  DB_DATABASE={names['DB_NAME']}")
    L.append(f"  DB_USERNAME={names['DB_USER']}")
    L.append(f"  DB_PASSWORD={pw['DB_PASSWORD']}")
    if db_publish:
        _ext = host if _pb in ("", "0.0.0.0", "::", "[::]") else _pb
        L.append("")
        L.append(f"  {DIM}Desde afuera (tu maquina o el cliente):{RESET}")
        L.append(f"  DB_CONNECTION={db_driver}")
        L.append(f"  DB_HOST={_ext}")
        L.append(f"  DB_PORT={_pp}")
    L.append("")

    L.append(f"{bar}")
    L.append(f"Credenciales en  {pdir}/.env   {DIM}(0600){RESET}")
    if not creds_only:
        L.append(f"Gestion         appctl {project} creds")
        L.append(f"Rotar           appctl {project} rotate sftp")
    L.append("")
    return "\n".join(L)


def render_rotate(project: str, what: str, creds: list[tuple[str, str]],
                  host: str, port: int) -> str:
    L = ["", f"{BOLD}{what} rotada en {project}{RESET}", "-" * 40]
    first_user = creds[0][0] if creds else "?"
    for user, pw in creds:
        L.append(f"  User    {user}")
        L.append(f"  Pass    {pw}")
    if what == "SFTP":
        L.append(f"  sftp -o PreferredAuthentications=password -P {port} "
             f"{first_user}@{host}")
    L.append("")
    L.append(f"  {DIM}La anterior deja de funcionar de inmediato.{RESET}")
    L.append("")
    return "\n".join(L)


def render_list(proj_dir: str) -> str:
    d = Path(proj_dir)
    # Directorio inexistente es una configuracion mal puesta, no "no hay
    # proyectos". Decir 'no hay proyectos en /srv/appctl' cuando ese path no
    # existe hizo perder tiempo: los proyectos estaban en otro lado.
    if not d.exists():
        return ("\n  el directorio de proyectos no existe: " + str(d) +
                "\n\n"
                "  no es que no haya proyectos: appctl esta mirando un path que\n"
                "  no existe. Cada instalacion tiene el suyo.\n\n"
                "    export APPCTL_PROJECTS=/ruta/donde/estan\n"
                "  o, para que sea permanente y no haya que exportar cada vez:\n"
                "    echo 'APPCTL_PROJECTS=/ruta/donde/estan' | sudo tee /etc/default/appctl\n")
    rows = []
    sin_permiso = []
    for pdir in sorted(d.iterdir()) if d.is_dir() else []:
        if not pdir.is_dir() or pdir.name.startswith("."):
            continue
        sf = pdir / "state.json"
        # Un directorio 0700 de otro usuario: exists() devuelve False sin
        # fallar, y el listado terminaba diciendo 'no hay proyectos' con dos
        # puertos registrados y un stack andando. Decir que no hay es peor
        # que no listar: hay que decir que no se puede leer.
        if not os.access(pdir, os.R_OK | os.X_OK):
            sin_permiso.append(pdir.name)
            continue
        if not sf.exists():
            continue
        try:
            st = json.loads(sf.read_text())
        except (OSError, ValueError):
            sin_permiso.append(pdir.name)
            continue
        rows.append(st)
    if not rows and sin_permiso:
        L = ["", f"  no hay proyectos visibles, pero {len(sin_permiso)} existen",
             "  y este usuario no los puede leer:", ""]
        for n in sorted(sin_permiso)[:8]:
            L.append(f"    {n}")
        L += ["", "  estan como root. Se crean asi con 'sudo appctl', y despues",
              "  el usuario normal no entra al directorio ni lee el registro.", "",
              f"    sudo chown -R $(id -un):$(id -gn) {d}/*", "",
              "  appctl no necesita sudo: con estar en el grupo docker alcanza."]
        return "\n".join(L) + "\n"
    if not rows:
        return "\n  no hay proyectos en " + str(d) + "\n"
    L = ["", f"  {'PROYECTO':<20} {'STACK':<26} {'APP':<7} {'SFTP':<7} {'DB':<10}", "  " + "-" * 74]
    for st in rows:
        sftp = str(st.get("sftp_port") or "-")
        db = (st.get("db") or {}).get("name") or "-"
        L.append(f"  {st['project']:<20} {st['stack']:<26} "
                 f"{st.get('app_port','-'):<7} {sftp:<7} {db:<10}")
    L.append("")
    return "\n".join(L)


def fmt_limites(mem_bytes, nano_cpus) -> str:
    """'512M / 1.00 cpu' a partir de lo que dice docker.

    En docker 0 es sin techo, asi que 0 se muestra como tal y no como 0M,
    que se leeria como "no tiene nada".
    """
    try:
        m = int(mem_bytes)
    except (TypeError, ValueError):
        m = 0
    try:
        c = int(nano_cpus)
    except (TypeError, ValueError):
        c = 0
    if m <= 0 and c <= 0:
        return "sin limite"
    sm = f"{m//1024//1024}M" if m > 0 else "-"
    sc = f"{c/1e9:.2f} cpu" if c > 0 else "-"
    return f"{sm} / {sc}"


def render_info(st: dict, env: dict, ps_out: str, limits=None,
                db_publish: str | None = None) -> str:
    L = ["", f"  {BOLD}{st['project']}{RESET}  {DIM}creado {st.get('created','?')}{RESET}", ""]
    L.append(f"  Stack      {st['stack']}")
    L.append(f"  Host       {st['host']}")
    L.append(f"  App        http://{st['host']}:{st['app_port']}")
    if st.get("sftp_port"):
        L.append(f"  SFTP       {st['host']}:{st['sftp_port']}  user {env.get('SFTP_USER')}")
    _dbn = (st.get("db") or {}).get("name", "-")
    _decl = st.get("db_published_port")
    if db_publish:
        # Lo que dice docker: la base esta publicada en el host. Se muestra la
        # direccion que le sirve a un cliente (el FQDN cuando el bind es
        # 0.0.0.0) y el alcance, porque 0.0.0.0 y 127.0.0.1 no son lo mismo.
        _pb, _, _pp = db_publish.rpartition(":")
        L.append(f"  DB         {_dbn}  {YELLOW}publicada en "
                 f"{addr_publicada(_pb, _pp, st.get('host', '?'))}{RESET}"
                 f"{YELLOW}{alcance_bind(_pb)}{RESET}"
                 f"  {DIM}(cerrala: appctl {st['project']} db unexpose){RESET}")
    elif _decl:
        # El state y el contenedor no coinciden: decirlo, no taparlo. Un
        # cliente que cree que su base esta abierta y no lo esta pierde mas
        # tiempo que uno al que le avisan.
        L.append(f"  DB         {_dbn}  {YELLOW}el state dice publicada en "
                 f"{_decl} pero el contenedor NO la publica{RESET}"
                 f"  {DIM}(appctl {st['project']} db expose {_decl}){RESET}")
    else:
        L.append(f"  DB         {_dbn}  {DIM}(red interna){RESET}")
    L.append("")
    L.append(f"  {BOLD}Componentes{RESET}   {', '.join(st.get('components', []))}")
    L.append(f"  {BOLD}Imagenes{RESET}")
    for k, v in st.get("tags", {}).items():
        L.append(f"    {k:<10} {v}")
    L.append("")
    if ps_out.strip():
        L.append(f"  {BOLD}Servicios{RESET}")
        for line in ps_out.strip().splitlines():
            L.append(f"    {line}")
    if limits:
        L.append("")
        L.append(f"  {BOLD}Limites{RESET}  {DIM}(lo aplicado ahora, en caliente incluido){RESET}")
        for svc in ("app", "db"):
            L.append(f"    {svc:<4} {limits.get(svc) or '-'}")
    L.append("")
    return "\n".join(L)
