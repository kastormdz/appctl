# appctl

Un comando y tenés el server del cliente andando.

```bash
appctl acme php psql sftp
```

Levanta nginx + PHP 8.5 + PostgreSQL 18 + SFTP, publica solo los puertos que
hace falta (el de la app y el del SFTP), deja la base de datos inalcanzable
desde afuera, y te devuelve un resumen con todo lo que hay que passarle al
developer.

[English](README.en.md)

---

## Qué es y qué no es

Es un provisionador. Le decís qué quiere el cliente y te da un stack corriendo,
que se borra entero con un comando.

No es un PaaS. No hay panel web, ni deploys por git push, ni billing, ni
multiserver. Si eso es lo que necesitás, Coolify y Dokploy ya lo hacen, y los
vas a tener que operar y actualizar vos. Acá el problema es otro: que el cliente
pida un PHP con PostgreSQL y SFTP y que eso exista hoy, no en tres días.

Cada cliente es un directorio con un `compose.yaml`. Nada más.

---

## Requisitos del servidor

Un Linux con Docker y Compose v2, y un usuario que pueda hablar con el daemon:

| | |
|---|---|
| Docker | 29.8.2 (probado) |
| Compose | v2.40.3 (probado) |
| RAM | ~1 GB por cliente (su app + su base) |
| sudo | sin password, o un wrapper |

Cada cliente lleva **su propia base en su propio contenedor**. No hay una base
compartida entre todos. Es lo que hace que el aislamiento sea real y no un
`GRANT` que promete. Sale en RAM; mientras los clientes sean pocos, sobra.

El CLI corre en el host, al lado de Docker. Necesita el daemon y el socket, y el
`flock` del registro tiene que estar en el mismo filesystem que el registro.

Del TLS no se ocupa: eso lo termina un reverse proxy adelante, que no es parte
de esto. Los stacks sirven HTTP en un puerto del host y el que los publica
decide el certificado.

---

## Layout en el host

Todo cuelga de un directorio configurable, con un subdirectorio por cliente:

```
$APPCTL_PROJECTS/<proyecto>/          # uno por cliente
├── compose.yaml
├── .env                                 # 0600
├── nginx/site.conf
├── db/init/                             # scripts de initdb
├── db/data/                             # volumen de la base
└── state.json
```

El default es `/srv/appctl`, pero lo importante es dónde: **en el mismo
filesystem que el root de Docker**. Un proyecto de cliente separado del lugar
donde viven sus datos es un restore que no cierra.

El registro de puertos va aparte, en `$APPCTL_HOME` (por defecto
`/srv/appctl/.appctl`), oculto, para que listar los proyectos muestre solo
clientes.

| Variable | Default | Para qué |
|---|---|---|
| `APPCTL_PROJECTS` | `/srv/appctl` | raíz de los proyectos |
| `APPCTL_HOME` | `/srv/appctl/.appctl` | registro de puertos y versiones |
| `APPCTL_HOST` | `localhost` | el host que sale en el resumen |
| `HTTPS_PROXY` | — | hace falta si la red no llega a los repos |

---

## Sintaxis

```bash
appctl <proyecto> [componentes...] [opciones]
```

## Los stacks

Cada stack es un directorio bajo `stacks/`, con su `compose.tmpl.yaml` y una
imagen propia (Dockerfile, entrypoint, nginx) donde hace falta. Agregar uno
nuevo es copiar un directorio y ajustar los placeholders.

Hay **4**:

| Stack | App | DB | Servicios | Notas |
|---|---|---|---|---|
| `php-postgres-sftp` | PHP 8.5 + nginx | PG 18 | 3 | el default |
| `php-mysql-sftp` | PHP 8.5 + nginx | MariaDB 11.8 | 3 | |
| `nextjs-postgres-sftp` | Node 22 + Next | PG 18 | 3 | build multi-stage, `.next` |
| `tomcat-postgres-sftp` | Tomcat 11 + nginx | PG 18 | 4 | nginx va aparte |

Los que faltan (los `mysql` de nextjs y tomcat, y el stack con `cron` para
workers de cola) son directorios nuevos. El CLI ya los pide por nombre; lo que
no existe todavía es el directorio. No está-built por diseño: cada uno se
agrega cuando alguien lo pide de verdad, y así el repo no carga con stacks que
nadie probó.


## Stack `tomcat`

Tomcat cambia la forma del stack, porque **no es PHP**: no hay `.php` que
servir, hay un `.war` que compilar y deployar. Tres diferencias concretas contra
`php`:

| | `php` | `tomcat` |
|---|---|---|
| código | `/var/www/html` montado | `/usr/local/tomcat/webapps/<proyecto>.war` |
| build | el cliente sube el código ya hecho | `mvn package` / `gradle build` → el `.war` |
| descentrado | nginx → `php-fpm:9000` | nginx → `tomcat:8080` |

El compose lleva **4 servicios**: `app` (tomcat), `nginx`, `db` y `sftp`. Acá
nginx **sí** va aparte, porque tiene que terminar el TLS y hablar con el
`catalina.sh` que corre en otro proceso. En PHP nginx y FPM viven juntos; en
Tomcat no pueden.

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
compose y en `state.json`**. Un contenedor que corre no se actualiza solo,
nunca. Actualizar es `appctl acme upgrade`, y es explícito a propósito.

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
`--dry-run` · `--yes`

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

`php:8.5-fpm-alpine` pesa 150 MB contra 732 MB de bookworm, cinco veces menos.
Eso solo decide cuántos clientes entran en el disco. El stack de PHP usa
alpine; si un cliente necesita una extensión que no compila ahí (GD con
librerías del sistema, por ejemplo), se cae a bookworm y se acepta el costo.

Tomcat: los tags con `jre` en el nombre **no existen** (`11-jdk25-temurin-jre17-noble`
no baja), solo los `jdk`. Y hay 9, 10 y 11 disponibles, que es exactamente lo que
permite preguntar la versión al cliente en vez de suponer.

## El stack por defecto (`php psql sftp`)

Un `compose.yaml` con **tres** servicios (`app`, `db`, `sftp`) y **dos** redes.
Lo interesante son las redes:

```yaml
networks:
  # con salida a internet: la app llama a una pasarela de pagos, a un SMTP,
  # a la API de un whatsapp. Sin esto, el cliente no puede cobrar.
  egress:
    driver: bridge
  # SIN GATEWAY. La garantía de que la DB no se ve desde afuera, hecha con
  # machinery y no con "no le puse ports:". Nadie la alcanza, ni siquiera
  # un contenedor de otro proyecto.
  data:
    driver: bridge
    internal: true
```

`app` está en las dos redes: sale a internet para `composer` y `packagist`, y
llega a la base solo por `data`. `db` está solo en `data`.

Si las dos fueran una, la base sería alcanzable desde cualquier cosa que pueda
enrutar al host. Esa es toda la razón de que sean dos.

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

Un stack `php + postgres` ronda 800 MB de imagen. Con datos, logs y backups,
cada cliente se va a 1.5-2 GB en la práctica. Decime cuántos clientes querés
sostener y te digo si el disco da; si no da, se agranda o se mueven los
volúmenes de datos afuera.

---

## La base de datos: los comandos que la mueven

La DB **nunca se expone sola**. Vive en una red `internal:true` sin puerto
publicado; el chequeo `create` lo verifica de verdad: lo prueba desde otro contenedor,
no leyendo la config.

### Exponerla

```bash
appctl <p> db expose 5432                     # solo desde 127.0.0.1
appctl <p> db expose 5432 --cidr 10.20.0.0/16  # abrir a una red
```

El default es `127.0.0.1`, no una red. Con un `/8` cualquiera, cualquier LAN que
llegue al puerto entra, y quien llama al comando no tiene por qué saber que
la base quedó abierta a medio entorno. Para una red, `--cidr` explícito.

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
**y** `CREATE USER` global. MariaDB rechaza `GRANT OPTION ON db.*` con 1064
porque no es un privilegio: es una opción de `GRANT`. El alcance global no
molesta acá porque cada base está en su red y no comparte nada con otros
clientes, y el aislamiento lo da la red. En un MySQL con varias bases en el
mismo servidor, sí.

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

- `flock` sobre `registry.json` durante todo `create`/`destroy`. Sin eso, dos
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
docker run --rm --network host postgres:18-alpine \
  pg_isready -h 127.0.0.1 -p 5432   # tiene que fallar
```

Que el puerto no esté en el YAML no alcanza: hay que comprobar que un cliente
desde afuera muere. Si una imagen de la base un día trae un `ports:` puesto, eso
frena en la creación y no tres meses después.

---

## Estructura del repo

```
appctl/
├── bin/appctl                 # el CLI, stdlib y nada más
├── lib/
│   ├── gen.py                 # renderiza el compose y el .env
│   ├── ports.py               # registry de puertos + flock
│   ├── dbtool.py              # dump / restore / expose / grant
│   ├── smoke.py               # los 9 checks
│   └── summary.py             # el resumen para el developer
├── stacks/                    # 4 stacks, uno por directorio
├── tests/
│   ├── test_init_sql.py       # ejecuta los init y valida el SQL que producen
│   ├── test_root_pw.py        # parsea la línea del log de mariadb
│   ├── test_security.py       # el compose no publica la DB
│   ├── test_readme.py         # cada claim del README contra el codigo
│   └── check_names.py         # AST: llamadas sin definir, defs duplicadas
└── docs/                      # (privado, no en este repo)
```

`tests/test_init_sql.py` es el que más rindió. Un init corre una sola vez, cuando
se crea el volumen; si falla ahí, no hay reintento y el proyecto queda con una
base sin los tres usuarios para siempre. El test corre el init de verdad con
`psql`/`mariadb` sustituidos por un binario que hace `cat`, y mira el SQL que
llega. Sin base de datos de por medio.

`tests/test_readme.py` es el que evita que este README mienta. Extrae cada
afirmación verificable (los stacks, los flags, los subcomandos, las versiones,
los paths por defecto, las redes) y la contrasta contra los archivos. Existió
porque este README mentía: decía que había 8 stacks y había 4, y listaba dos módulos de lib/ que nunca se escribieron, y documentaba un flag de precarga de
environment que el parser nunca 정의. Nada de eso se ve leyendo el documento; se ve
corriendo el test.


---|---|
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
