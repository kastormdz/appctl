# appctl

Provisionador de servidores de aplicaciones por cliente. Un comando, un stack
dockerizado autocontenido, un resumen listo para pasarle al developer.

```bash
appctl acme php psql sftp
```

Genera un proyecto `acme` con nginx + PHP 8.5 + PostgreSQL 18 + SFTP, publica
solo el puerto de la app y el del SFTP, deja la base de datos inalcanzable desde
la red, y devuelve el resumen con las credenciales.

---

## Qué es y qué no es

**Es** un provisionador. Le decís qué quiere el cliente y te da un stack corriendo,
autocontenido y destruible con un comando.

**No es** un PaaS. No hay panel web, ni multi-servidor, ni billing, ni git push
deploys. Esas cosas las agregan Coolify/Dokploy; acá serían features que vos
tenés que operar, actualizar y asegurar, sin resolver el problema real.

El estado de cada cliente es un directorio con un `compose.yaml`. Nada más.

---

## Requisitos del servidor

Un host Linux con Docker + Compose v2 y un usuario con acceso al daemon:

| | |
|---|---|
| Docker | 29.8.2 (probado), 24+ debería servir |
| Compose | v2.40.3 (probado) |
| RAM | ~1 GB por cliente (su app + su DB) |
| sudo | NOPASSWD, o un wrapper: `destroy` borra archivos que crea el contenedor |

**Cada cliente lleva su propia base en su propio contenedor.** No hay una base
compartida: es lo que hace que el aislamiento sea real en vez de prometido con
GRANTs. El costo es RAM; a escala chica sobra, y la primera vez que los clientes
sumen memoria se mide.

El CLI corre **en el host, al lado de Docker**: necesita el daemon y el socket, y
el `flock` del registry tiene que vivir en el mismo filesystem que el registry.
No es un panel web ni un PaaS: es una CLI que crea stacks. La API HTTP no expone
nada por defecto.

TLS lo termina un reverse proxy **por delante**, que no es parte de esto. Los
stacks sirven HTTP en un puerto del host y quien los publica decide el
certificado; `appctl` no sabe de certificados.

---

## Layout en el host

Todo vive bajo un directorio configurable (`APPCTL_PROJECTS`, por defecto
`/srv/appctl`), con un subdirectorio por cliente:

```
$APPCTL_PROJECTS/<proyecto>/          # un directorio por cliente
├── compose.yaml
├── .env                                 # 0600
├── nginx/site.conf                      # montado, no baked en la imagen
└── state.json
```

**Los proyectos van en el mismo filesystem que los volúmenes Docker.** Un
proyecto de cliente fuera del filesystem donde viven sus datos es una receta para
un restore que no cierra. Por eso el default no es `/opt` ni `/srv` sino el
directorio donde ya está el root de Docker.

```
/usr/local/bin/appctl                    # el CLI (stdlib only)
$APPCTL_PROJECTS/.appctl/registry.json   # puerto→cliente, con flock
$APPCTL_PROJECTS/.appctl/versions.json   # tag flotante de las imágenes
```

El registry va en `.appctl/` (oculto, fuera del alcance de los directorios de
proyecto) para que `ls $APPCTL_PROJECTS/` muestre solo clientes.

Ajustables por entorno, para que el repo sirva en dos lugares:

| Variable | Default | Qué es |
|---|---|---|
| `APPCTL_PROJECTS` | `/srv/appctl` | raíz de los proyectos |
| `APPCTL_HOME` | `/srv/appctl/.appctl` | registry y versiones |
| `APPCTL_HOST` | `localhost` | host que aparece en el resumen |
| `HTTPS_PROXY` | — | hace falta si la red no llega a los repos |

---

## Sintaxis

```bash
appctl <proyecto> [componentes...] [opciones]
```

## Los stacks

Cada stack es un directorio bajo `stacks/` con `Dockerfile` (si hace falta),
`compose.tmpl.yaml`, `env.tmpl` y `nginx.tmpl`. Agregar uno nuevo = copiar un
directorio. Hay **8**:

| Stack | App | DB | Servicios | Notas |
|---|---|---|---|---|
| `php-postgres-sftp` | PHP 8.5 + nginx | PG 18 | 3 | **el de hoy** |
| `php-mysql-sftp` | PHP 8.5 + nginx | MariaDB 11 | 3 | el más pedido |
| `nextjs-postgres-sftp` | Node 22 + Next | PG 18 | 3 | build multi-stage, `.next` |
| `nextjs-mysql-sftp` | Node 22 + Next | MariaDB 11 | 3 | |
| `tomcat-postgres-sftp` | Tomcat 11 + nginx | PG 18 | **4** | nginx aparte |
| `tomcat-mysql-sftp` | Tomcat 11 + nginx | MariaDB 11 | **4** | nginx aparte |
| `php-postgres` | PHP 8.5 | PG 18 | 2 | sin sftp |
| `php-postgres-cron` | PHP 8.5 + supervisor | PG 18 | 3 | queue workers |

`php-postgres-cron` existe porque el 90% de los pedidos de PHP con
Laravel/WordPress en 30 días necesita un worker de cola. Sin él, el primer
"necesito correr un cron" es un ticket de soporte.

## Stack `tomcat`

Tomcat cambia la forma del stack, porque **no es PHP**: no hay `.php` que
servir, hay un `.war` que compilar y deployar. Tres diferencias concretas contra
`php`:

| | `php` | `tomcat` |
|---|---|---|
| código | `/var/www/html` montado | `/usr/local/tomcat/webapps/<proyecto>.war` |
| build | el cliente sube el código ya hecho | `mvn package` / `gradle build` → el `.war` |
| descentrado | nginx → `php-fpm:9000` | nginx → `tomcat:8080` |

El compose lleva **4 servicios**: `app` (tomcat), `nginx`, `db`, `sftp` — o sea
que acá nginx **sí** es un servicio separado, porque el reverse proxy tiene que
terminar TLS/routing y hablar con el `catalina.sh` que corre aparte. En PHP,
nginx y FPM viven juntos; en Tomcat, no.

Imagen: `tomcat:11-jdk25-temurin-noble` (Tomcat 11 = Jakarta EE 10, requiere Java
17+). **OJO:** Tomcat 10 fue Jakarta y **no** es compatible binariamente con
Tomcat 9 (`javax.*` → `jakarta.*`). Un `.war` que hoy compila en 9 no corre en 11
sin tocar imports. Hay que preguntar la versión de Tomcat al cliente, no asumir
la última.

## Stack `mysql`

`mysql` es alternativa a `psql`, nunca adicional. Con `php` lo normal es
**MariaDB 11.8** (GPL, sin fricción de licencia, mejor embed); MySQL 8.4
(oracle) solo si el cliente lo pide explícitamente. Con `tomcat` o Next.js,
MySQL 8.4 sí tiene más sentido.

## El registry: qué versión se instala

Los tags se resuelven en `create`, no están escritos en el código:

```
appctl acme php psql sftp          # PHP 8.5.11 + PostgreSQL 18.6 + atmoz/sftp:alpine
appctl acme php psql sftp --php 8.3   # fijado a 8.3
```

El tag flotante vive en `$APPCTL_HOME/versions.json`, con un `lock` de
escritura. Regla: **el tag exacto se resuelve una vez y se escribe en el
compose y en `state.json`**. Un contenedor que corre no se actualiza solo —
nunca. Actualizar es `appctl acme upgrade`, explícito.

Razón de la regla: `restart: unless-stopped` re-pulled no hace nada (no
re-pullea), pero un `docker compose pull` en un `up` futuro sí. Si el tag
flotara, el cliente A sube a PHP 8.6 un martes, sin querer, porque alguien
corrió `up`. Fijado en el YAML, eso no puede pasar.

## Los componentes

| Componente | Qué agrega |
|---|---|
| `php` | PHP-FPM 8.5 + nginx + supervisor, volumen de código |
| `nextjs` | Node 22 + build Next.js, volumen de app |
| `tomcat` | Tomcat 11 + JDK 25, reverse proxy nginx aparte, `.war` en `webapps/` |
| `psql` / `mysql` | PostgreSQL 18 / MariaDB 11 (o MySQL 8.4), red privada |
| `sftp` | atmoz/sftp, puerto dedicado, volumen compartido con la app |
| `cron` | supervisor con crontab montado |

```
appctl acme php psql sftp
appctl acme php mysql
appctl nextjs-proyecto nextjs psql sftp --app-port 3001
```

**Opciones:** `--app-port N` (default: primer libre desde 8001) · `--sftp-port N`
(default: 2221) · `--host NAME` (default: `$APPCTL_HOST`) ·
`--env .env.example` (semilla para precargar) · `--dry-run` · `--yes`

**Invariantes que valida antes de crear:**

- `proyecto` es `[a-z0-9][a-z0-9-]{1,30}[a-z0-9]`
- hay **al menos un runtime** (php o nextjs) y **al menos un servicio** (psql o mysql o sftp)
- el puerto de app y el de sftp están libres (registry **y** `ss -ltnp` real)
- el proyecto no existe ya
- el stack resultante existe en `stacks/`

---

## Versiones verificadas (pull real contra Docker Hub, 2026-10-01)

No de memoria ni del sitio de Docker Hub: bajadas una por una.

| Imagen | Tag verificado | Tamaño |
|---|---|---|
| PHP | `php:8.5-fpm-bookworm` | 732 MB |
| PHP | `php:8.5-fpm-alpine` | **150 MB** ← el que usa el stack |
| PostgreSQL | `postgres:18-alpine` | 433 MB |
| MariaDB | `mariadb:11.8` | 458 MB |
| Node | `node:22-alpine` | 238 MB |
| nginx | `nginx:stable-alpine` | 93.6 MB |
| SFTP | `atmoz/sftp:alpine` | 32.1 MB |
| Tomcat | `tomcat:11-jdk25-temurin-noble` | 602 MB |
| Tomcat | `tomcat:10.1-jdk21-temurin-noble` | 729 MB |
| Tomcat | `tomcat:9-jdk21-temurin-noble` | 730 MB |

`php:8.5-fpm-alpine` pesa **150 MB contra 732 MB** de bookworm — 5x menos. Es la
diferencia entre 15 clientes y 30 en un disco de 32 GB. El stack de PHP usa
alpine; si un cliente necesita extensiones que no compilan en alpine (o GD con
librerías del sistema), se cae a bookworm y se acepta el costo.

Tomcat: los tags con `jre` en el nombre **no existen** (`11-jdk25-temurin-jre17-noble`
no baja), solo los `jdk`. Y hay 9, 10 y 11 disponibles, que es exactamente lo que
permite preguntar la versión al cliente en vez de suponer.

## El stack por defecto (`php psql sftp`)

Un `compose.yaml` con **dos** redes y **tres** servicios.

```yaml
name: appctl-acme

services:
  app:
    image: appctl/php-nginx:8.3
    container_name: acme_app
    restart: unless-stopped
    environment:
      DB_HOST: db
      DB_PORT: "5432"
      DB_NAME: acme_db
      DB_USER: acme
      DB_PASSWORD: ${DB_PASSWORD}
      APP_KEY: ${APP_KEY}
    volumes:
      - ./nginx/site.conf:/etc/nginx/conf.d/default.conf:ro
      - code:/var/www/html
    networks: [egress, data]
    deploy:
      resources:
        limits: {cpus: "1.0", memory: 1G}

  db:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_DB: acme_db
      POSTGRES_USER: acme
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - dbdata:/var/lib/postgresql/data
    networks: [data]          # <- data es internal: no gateway, no ruta
    # SIN ports:. La DB es inalcanzable desde el host y desde el exterior.

  sftp:
    image: atmoz/sftp
    command: ${SFTP_USER}:${SFTP_PASSWORD}:::upload
    volumes:
      - code:/home/${SFTP_USER}/upload
    ports:
      - "${SFTP_PORT}:22"
    networks: [egress, data]
    deploy:
      resources:
        limits: {cpus: "0.25", memory: 128M}

networks:
  egress:
    driver: bridge             # con salida a internet: la app llama a APIs externas
  data:
    driver: bridge
    internal: true             # <- LA GARANTÍA: sin gateway, sin entrada ni salida

volumes: {code: {}, dbdata: {}}
```

### Por qué dos redes

Pedir `internal: true` en la red de todo el stack rompe las apps: sin gateway la
app no puede llamar a una pasarela de pagos, ni a un SMTP, ni a la API de un
whatsapp. La app va en `egress` (internet) **y** en `data` (la DB). La DB va
**solo** en `data`.

`internal: true` es la garantía de "solo se ve la DB adentro", hecha con
machinery y no con `ports:` ausente. Sin gateway, ningún contenedor ajeno alcanza
esta red, aunque un día alguien edite el compose a mano.

El puerto de la app se publica vía `ports: "${APP_PORT}:80"` en el servicio `app`.
nginx queda **dentro** del contenedor: el cliente tiene su servidor web listo y es
un contenedor menos que operar. Trade-off asumido: si pide Apache, es otro stack.

---

## Qué genera y con qué permisos

| Objeto | Nombre | Permisos |
|---|---|---|
| Base de datos | `<proyecto>_db` | — |
| Rol de la app | `<proyecto>` | `ALL` sobre `_db`, `CONNECT` en `_db` |
| Rol de migraciones | `<proyecto>_mig` | `ALL` sobre `_db` (para `artisan migrate` de Laravel) |
| Rol de lectura | `<proyecto>_ro` | `SELECT` sobre `_db` |
| SFTP user | `<proyecto>` | chroot a su home, `upload/` escribible |
| `.env` | 0600, root:root | secretos en claro, nunca en el YAML |

En MySQL: `GRANT ALL ON \`acme_db\`.*` para `<proyecto>` y `<proyecto>_mig`, más
un `<proyecto>_ro` con `SELECT`. En PostgreSQL: `CREATE ROLE` + `CREATE DATABASE
OWNER` + `REVOKE CONNECT ON DATABASE ... FROM PUBLIC` (sin eso, cualquier rol
puede conectarse a la base de un cliente).

### Roles separados, y por qué

El rol de la app **no** necesita `CREATE SCHEMA` ni `DROP`. `artisan migrate` sí.
Mezclar los dos significa que un SQL injection en la app puede borrar el schema.
Son dos roles distintos, dos passwords distintos, y el developer recibe el de
migraciones aparte, con instrucción de usarlo solo para migrar.

---

## El resumen (output de `create`)

```
╭──────────────────────────────────────────────────────────────╮
│  Proyecto:  acme                                              │
│  Host:      apps.example.com                                  │
╰──────────────────────────────────────────────────────────────╯

  App        http://apps.example.com:8001
  SFTP       sftp://acme@apps.example.com:2221

  Base de datos (solo red interna)
    DB        acme_db
    User      acme              Pass  xK7#mQ2v...
    User      acme_mig          Pass  pR4$nW9z...   ← migraciones
    User      acme_ro           Pass  bT8&hJ1s...   ← solo lectura

  SFTP
    User      acme              Pass  ...
    (chroot, sube a /home/acme/upload/)

  Credenciales en  $APPCTL_PROJECTS/acme/.env  (0600)
```

Se imprime **una sola vez**. Después se lee con `appctl <proyecto> creds`.

---

## Disco: el límite real

`/data` = 32 GB. Un stack `php + postgres` ronda 800 MB de imagen; con datos,
logs y backups, cada cliente se come 1.5-2 GB realista. **~15 clientes con
margen**, no los 30 que salen de hacer la cuenta optimista con la RAM.

Cuando se llegue a 10, se mide el uso real y se decide: ampliar el LVM, o mover
los volúmenes de datos a espacio externo (local, S3, lo que sea). No antes — con
10 clientes y 20 GB libres no hay nada que decidir.

## La base de datos: los comandos que la mueven

La DB **nunca se expone sola**. Vive en una red `internal:true` sin puerto
publicado; el chequeo `create` lo verifica de verdad: lo prueba desde otro contenedor,
no leyendo la config.

### Exponerla

```bash
appctl <p> db expose 5432                     # solo desde 127.0.0.1
appctl <p> db expose 5432 --cidr 10.20.0.0/16  # abrir a una red
```

El default es `127.0.0.1`, no una red. Con `10.0.0.0/8` cualquier LAN que llegue
al puerto entra, y quien llama al comando no tiene por qué saber que la base
quedó abierta a medio entorno. Para una red, `--cidr` explícito.

`appctl <p> db unexpose` cierra. Publicar el puerto **no** hace que la base
deje de estar en su red interna: es una publicación del host, no un permiso.

### Backups

```bash
appctl <p> db dump                 # a un archivo, ambos motores
appctl <p> db restore <archivo>
```

En Postgres el dump va con `--no-owner --no-acl` y **sin `--clean`**: un
`--clean --if-exists` genera `DROP ROLE` del origen y el restore muere con
`FATAL: role "x" does not exist` cuando se restaura en un clon.

### Un usuario nuevo

```bash
appctl <p> db grant nombre --rol readonly
```

En MariaDB el usuario de migración necesita `WITH GRANT OPTION` sobre la base
**y** `CREATE USER` global — MariaDB rechaza `GRANT OPTION ON db.*` con 1064
porque no es un privilegio, es una opción de `GRANT`. El alcance global es
seguro acá porque cada base está en su red y no comparte nada con otros
clientes: el aislamiento lo da la red. En un MySQL con varias bases en el mismo
servidor, no.

---

## Comandos

```bash
appctl <proyecto> <componentes...>   # crear
appctl list                          # tabla: proyecto | app | sftp | db | estado
appctl <proyecto> info               # urls + estado de cada servicio
appctl <proyecto> creds              # re-revela el .env (queda en el scrollback)
appctl <proyecto> logs [-f] [--app|--db|--sftp]
appctl <proyecto> shell              # exec bash en el contenedor app
appctl <proyecto> psql               # psql ya conectado, sin password
appctl <proyecto> restart
appctl <proyecto> rotate <sftp|db|mig>
appctl <proyecto> destroy [--keep-data]
appctl doctor                        # estado del host
```

---

## Idempotencia y concurrencia

- `flock` sobre `registry.json` durante todo `create`/`destroy` — sin esto, dos
  altas en paralelo se clavan el mismo puerto sin enterarse.
- Reserva los **dos** puertos (app y sftp) en el registry antes de levantar nada.
- Verifica contra `ss -ltnp` real además del registry: el registry puede
  mentir si alguien levantó un contenedor a mano.
- `create` sobre un proyecto existente falla. `destroy` exige `--yes` o
  confirmación interactiva.

### La verificación que más importa

Después de `up -d`, el smoke test **comprueba que la DB no responde desde el
host**:

```bash
docker run --rm --network host postgres:16-alpine \
  pg_isready -h 127.0.0.1 -p 5432   # debe fallar / no conectar
```

No alcanza con que el puerto no esté en el YAML — hay que *comprobar* que un
cliente desde afuera muere. Si una imagen de la DB alguna vez trae un `ports:`,
eso frena en la creación y no tres meses después.

---

## Estructura del repo

```
appctl/
├── bin/appctl                 # el CLI (stdlib, sin deps)
├── lib/
│   ├── compose.py             # generación del compose
│   ├── secrets.py             # generación
│   ├── ports.py               # registry + flock + validación
│   ├── db.py                  # roles/grants PG y MySQL
│   ├── summary.py             # el resumen
│   └── smoke.py               # healthcheck + prueba de que la DB no está expuesta
├── stacks/
│   ├── php-postgres-sftp/     # Dockerfile + compose.tmpl + nginx.tmpl
│   ├── php-mysql-sftp/
│   ├── nextjs-postgres-sftp/
│   ├── nextjs-mysql-sftp/
│   ├── php-postgres-cron/     # el 6º: queue workers
│   └── php-postgres/
├── ansible/
│   ├── bootstrap.yml          # UNA vez: docker, ufw, fail2ban, sysctl, usuario
│   └── deploy-cli.yml         # instala /usr/local/bin/appctl
└── tests/
```

---

## Estado

Funcional y verificado end-to-end en PostgreSQL y MariaDB:

| | |
|---|---|
| `create` | 9/9 checks: contenedores healthy, DB responde, roles creados, password exigida, DB inalcanzable desde el host y desde otro contenedor |
| `clone` | datos, índices y credenciales nuevas; el clon es independiente del origen |
| `upgrade` | PHP 8.5 → 8.4 → 8.5, datos intactos |
| `db dump/restore` | Postgres y MariaDB |
| `db expose` | publica la DB en un puerto del host; por defecto solo desde `127.0.0.1`, con `--cidr` se abre a una red |
| `db grant` | alta de usuarios, con password propia |

Los stacks verificados: `php-postgres-sftp`, `php-mysql-sftp`,
`nextjs-postgres-sftp`, `tomcat-postgres-sftp`.

---
## El nombre del servidor en el summary

El summary dice el nombre de `$APPCTL_HOST`, no la IP. El developer lo copia y
funciona. Si mañana el servidor cambia de IP, el summary no queda viejo.

Si en algún momento se pide dominio por cliente, el compose ya queda preparado:
agregar un `labels:` de Traefik es una línea. No se implementa ahora porque no
fue pedido.
