#!/usr/bin/env python3
"""El arbol de despliegue en `creds` y el data dir fuera de la copia.

Gates:
 1. `creds` (creds_only=True) trae el arbol, con backend/, siga/ y el restart.
 2. Cada stack tiene SU arbol (o el generico), no el de otro.
 3. Los 3 stacks de Node excluyen DATA_DIRS del copiado (tar --exclude) y
    limpian una copia vieja en /app.
 4. El arranque de Next no depende de output:'standalone': hay start-next.sh
    con fallback a `next start`, y supervisord lo usa.
 5. DATA_DIRS llega al contenedor por el compose.
"""
import importlib.util
import pathlib
import re
import sys

RAIZ = pathlib.Path(__file__).resolve().parent.parent
fallos = []


def check(ok, msg):
    if not ok:
        fallos.append(msg)
        print(f"  FAIL - {msg}")
    else:
        print(f"  ok   - {msg}")


def cargar():
    spec = importlib.util.spec_from_file_location("summary", RAIZ / "lib/summary.py")
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def sin_ansi(s):
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


m = cargar()
NAMES = {"DB_NAME": "siga_db", "DB_USER": "siga", "DB_MIGRATION_USER": "siga_mig",
         "DB_READONLY_USER": "siga_ro", "SFTP_USER": "siga"}
PW = {k: "[REDACTED]" for k in ("DB_PASSWORD", "DB_MIGRATION_PASSWORD",
                               "DB_READONLY_PASSWORD", "SFTP_PASSWORD")}

# --- 1. el arbol sale en creds (que es lo que recibe el cliente) ------------
out = sin_ansi(m.render_create("siga", "nextjs-python-postgres-sftp",
                               "siga.mendoza.gov.ar", 3000, 2222, NAMES, PW,
                               "/srv/appctl/siga", creds_only=True))
check("Como desplegar" in out, "creds trae la seccion de despliegue")
check("/upload/" in out and "package.json" in out, "creds trae el arbol del frontend")
check("backend/" in out and "main.py" in out, "creds dice donde va el backend")
check("siga/" in out and "srv/sftp/upload/siga" in out, "creds dice donde van los datos")
check("output:'standalone'" in out, "creds menciona el standalone")
check("healthz" in out, "creds menciona los /healthz")
check("appctl siga restart" in out, "creds dice como aplicarlo (restart con el proyecto)")
check("[REDACTED]" in out, "creds sigue trayendo las credenciales")
check(out.count("Como desplegar") == 1,
      "una sola seccion de despliegue")

# --- 2. cada stack con su arbol --------------------------------------------
check("backend/" in m._arbol_para("nextjs-python-postgres-sftp"),
      "nextjs-python: arbol con backend/")
check("frontend/" in m._arbol_para("express-postgres-sftp"),
      "express: arbol con frontend/")
check("backend/" not in m._arbol_para("nextjs-postgres-sftp"),
      "nextjs: NO hereda el arbol de nextjs-python")
check("manager" in m._arbol_para("tomcat-postgres-sftp"),
      "tomcat: arbol del manager")
check(m._arbol_para("stack-que-no-existe") == "",
      "stack desconocido: sin arbol inventado")

# --- 3/4/5. los stacks -----------------------------------------------------
for st in ("nextjs-python-postgres-sftp", "nextjs-postgres-sftp", "express-postgres-sftp"):
    d = RAIZ / "stacks" / st
    ep = (d / "image/entrypoint.sh").read_text(encoding="utf-8")
    check("DATA_DIRS" in ep and "tar -C" in ep and "--exclude=./$_d" in ep,
          f"{st}: el copiado excluye DATA_DIRS")
    check('rm -rf "${APP_SRC:?}/${_d}"' in ep,
          f"{st}: limpia una copia vieja del data dir en /app")
    check('|*/*) continue' in ep or "|| continue" in ep,
          f"{st}: DATA_DIRS valida el nombre (no acepta paths)")
    comp = (d / "compose.tmpl.yaml").read_text(encoding="utf-8")
    check('DATA_DIRS: "${DATA_DIRS:-}"' in comp, f"{st}: DATA_DIRS en el compose")
    if st.startswith("nextjs"):
        chk = d / "image/start-next.sh"
        check(chk.exists(), f"{st}: existe start-next.sh")
        if chk.exists():
            s = chk.read_text(encoding="utf-8")
            check("next start" in s and "server.js" in s,
                  f"{st}: start-next.sh cae a next start sin standalone")
        sup = (d / "image/supervisord.conf").read_text(encoding="utf-8")
        check("start-next.sh" in sup and "/app/server.js" not in sup,
              f"{st}: supervisord arranca por start-next.sh")
        df = (d / "image/Dockerfile").read_text(encoding="utf-8")
        check("start-next.sh" in df, f"{st}: el Dockerfile copia start-next.sh")

# --- 5b. re-build por fecha, no por server.js ------------------------------
for st in ("nextjs-python-postgres-sftp", "nextjs-postgres-sftp"):
    ep2 = (RAIZ / "stacks" / st / "image/entrypoint.sh").read_text(encoding="utf-8")
    check("_debe_buildear" in ep2 and ".next/BUILD_ID" in ep2,
          f"{st}: re-buildea si el codigo es mas nuevo que el build")

# --- 5c. upgrade aplica de verdad (el .env no lo lee restart) --------------
app = (RAIZ / "bin/appctl").read_text(encoding="utf-8")
check("docker compose up -d" in app.split("def cmd_upgrade")[1][:9000],
      "upgrade aplica con up -d (re-lee el .env)")
check("se re-aplica con: appctl %s restart" not in app,
      "upgrade ya no manda a restart (que no re-lee el entorno)")
check("si tocaste el .env, se aplica con --yes" in app,
      "upgrade avisa como aplicar un cambio de .env")

# --- 6. los READMEs lo documentan -----------------------------------------
for readme in ("README.md", "README.en.md"):
    t = (RAIZ / readme).read_text(encoding="utf-8")
    check("DATA_DIRS" in t, f"{readme}: documenta DATA_DIRS")
    check("standalone" in t.lower(), f"{readme}: documenta el standalone")

print()
print(f"test_arbol_despliegue: {'OK' if not fallos else str(len(fallos)) + ' FALLAS'}")
sys.exit(1 if fallos else 0)
