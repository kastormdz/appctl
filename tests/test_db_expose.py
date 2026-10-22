#!/usr/bin/env python3
"""`db expose`: re-render sin marcadores crudos, YAML valido y puerto automatico.

Reproduce el bug medido en visualizadorminero (22/10): el re-render del expose
no sustituia los marcadores de S3 y el compose quedaba invalido
("yaml: line 26: could not find expected ':'").
"""
import importlib.machinery
import importlib.util
import pathlib
import sys

RAIZ = pathlib.Path(__file__).resolve().parent.parent
STACKS = ["php-postgres-sftp", "php-mysql-sftp", "nextjs-postgres-sftp",
          "nextjs-python-postgres-sftp", "express-postgres-sftp",
          "tomcat-postgres-sftp"]
fallos = []


def check(ok, msg):
    if not ok:
        fallos.append(msg)
        print(f"  FAIL - {msg}")
    else:
        print(f"  ok   - {msg}")


def cargar_cli():
    loader = importlib.machinery.SourceFileLoader("appctl_mod", str(RAIZ / "bin" / "appctl"))
    spec = importlib.util.spec_from_loader("appctl_mod", loader)
    assert spec is not None
    m = importlib.util.module_from_spec(spec)
    argv, sys.argv = sys.argv, ["appctl"]
    try:
        loader.exec_module(m)
    finally:
        sys.argv = argv
    return m


m = cargar_cli()
try:
    import yaml
except ImportError:
    yaml = None

SUBS = {
    "__PROYECTO__": "proj", "__APP_IMAGE__": "appctl/x:8.5-fpm-alpine-abcdef12",
    "__APP_PORT__": "8001", "__SFTP_PORT__": "2222", "__CRON__": "",
    "__PG_VERSION__": "18", "__MYSQL_VERSION__": "11.8",
    "__DB_PUBLISH__": "", "__S3__": "", "__S3_APP_ENV__": "",
}


def render_plano(stack):
    t = (RAIZ / "stacks" / stack / "compose.tmpl.yaml").read_text(encoding="utf-8")
    for k, v in SUBS.items():
        t = t.replace(k, v)
    return t


print("=== re-render del expose, por stack")
for st in STACKS:
    base = render_plano(st)
    try:
        out = m._set_publish(base, 3399, st, state={}, bind="0.0.0.0")
    except SystemExit:
        check(False, f"{st}: _set_publish aborto")
        continue
    marcadores = [w for w in out.split() if w.startswith("__") and w.endswith("__")]
    check(not marcadores, f"{st}: sin marcadores crudos ({marcadores[:2]})")
    d = None
    if yaml:
        try:
            d = yaml.safe_load(out)
            ok = True
        except Exception as e:
            ok = False
            print(f"        yaml: {e}")
        check(ok, f"{st}: el compose generado es YAML valido")
        if ok and d:
            interno = 3306 if "mysql" in st else 5432
            app = d["services"]["db"].get("ports") or []
            check(any(f":{interno}" in str(p) for p in app),
                  f"{st}: publica el puerto interno {interno}")
            check(any("3399" in str(p) for p in app), f"{st}: usa el puerto pedido")
    # S3: sin s3 en el estado -> vacio; con s3 -> aparece garage
    out_s3 = m._set_publish(base, 3399, st,
                            state={"s3": {"bucket": "b", "publish": None}},
                            bind="0.0.0.0")
    check("garage" in out_s3, f"{st}: con s3 en el estado, el servicio garage sobrevive")

print("\n=== webroot legible por la app (PHP)")
for st in ("php-postgres-sftp", "php-mysql-sftp"):
    ep = (RAIZ / "stacks" / st / "image/entrypoint.sh").read_text(encoding="utf-8")
    check('chmod 755 "${SFTP_CHROOT}/upload"' in ep,
          f"{st}: el upload queda 755 (php/nginx corren como app)")
    check('find "${WEBROOT}" -exec chmod a+rX' in ep,
          f"{st}: normaliza lo subido por SFTP a+rX")

print("\n=== puerto automatico")
check("nargs=\"?\", default=None" in (RAIZ / "bin/appctl").read_text(encoding="utf-8"),
      "el puerto es opcional en el parser")
check(hasattr(m, "_puerto_libre") and hasattr(m, "_puertos_en_uso"),
      "existen los helpers de puerto libre")
libre = m._puerto_libre(3306)
check(isinstance(libre, int) and 3306 <= libre < 65536, f"elige un puerto valido ({libre})")
check(not m._puerto_escuchando(libre), f"el elegido ({libre}) no esta escuchando")
en_uso = m._puertos_en_uso()
check(libre not in en_uso, "el elegido no esta en la lista de usados")

print()
print(f"test_db_expose: {'OK' if not fallos else str(len(fallos)) + ' FALLAS'}")
sys.exit(1 if fallos else 0)
