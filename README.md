# appctl

```bash
appctl acme php psql sftp
```

Eso levanta nginx, PHP 8.5, PostgreSQL 18 y SFTP, publica solo los dos puertos
que hacen falta, deja la base de datos inalcanzable desde afuera y te imprime
el resumen con todo lo que hay que passarle al developer.

[English](README.en.md)

---

## Qué es esto

Un provisionador. Le decis al cliente qué necesita y te devuelve un stack
corriendo, que se borra entero con un comando.

No es un PaaS. No hay panel web, ni deploys por `git push`, ni billing, ni
multiserver. Si eso es lo que buscas, Coolify y Dokploy ya lo hacen, y los vas a
tener que operar y actualizar vos. El problema que este repo resuelve es otro:
que el cliente pida un PHP con PostgreSQL y SFTP, y que eso exista hoy y no en
tres días.

Cada cliente es un directorio con un `compose.yaml`. Nada más.

---

## Requisitos del servidor

Un Linux con Docker y Compose v2, y un usuario que pueda hablar con el daemon:

| | |
|---|---|
| Docker | 29.8.2  |
| Compose | v2.40.3  |
| RAM | ~1 GB por cliente (su app y su base) |
| sudo | sin password, o un wrapper |
| `ss` | viene con iproute2 |
| `sshpass` | opcional: sin él se saltea el check de login por SFTP |

Cada cliente lleva **su propia base en su propio contenedor**. No hay una base
compartida entre todos, y eso es lo que hace que el aislamiento sea real en
lugar de un `GRANT` que promete. Sale en RAM; mientras los clientes sean pocos,
sobra.

El CLI corre en el host, al lado de Docker: necesita el daemon y su socket, y el
`flock` del registro tiene que estar en el mismo filesystem que el registro.

Del TLS no se ocupa. Eso lo termina un reverse proxy adelante, que no es parte
de esto: los stacks sirven HTTP en un puerto del host, y el que los publica
decide el certificado.

---

## Instalación

Sin dependencias de Python. Es stdlib y nada más: no hay `pip install`, ni
`requirements.txt`, ni `setup.py`.

Del host necesita `docker` con Compose v2, `ss`, `sudo` sin password para
`destroy`, y `sshpass` — este último solo para que el check de SFTP pueda
automatizar el login. Sin `sshpass` anda todo, el check se saltea y `doctor` te
avisa.

```bash
git clone https://github.com/kastormdz/appctl.git
cd appctl
./bin/appctl doctor
```

Si `doctor` dice OK, andas.

**El CLI busca `lib/` y `stacks/` relativos a sí mismo**, así que funciona desde
el clon y no desde donde lo hayas copiado. Instalalo donde lo clonaste:

```bash
sudo ln -s /ruta/donde/clonaste/appctl/bin/appctl /usr/local/bin/appctl
```

El symlink resuelve bien porque `Path(__file__).resolve()` sigue el enlace hasta
el archivo real, y las carpetas se encuentran igual.

**Copiar solo el binario no funciona.** Si hacés `cp bin/appctl /usr/local/bin/`
te va a fallar con un `ImportError` sobre `ports`, sin explicación: el binario
buscó `/usr/local/lib` y ahí no está.

Para probar sin tocar el sistema:

```bash
export APPCTL_PROJECTS=/tmp/appctl-probando
./bin/appctl acme php psql sftp
```

---

## Layout en el host

Todo cuelga de un directorio configurable, con un subdirectorio por cliente. Se
define con `APPCTL_PROJECTS`, y si no está en el entorno se lee de
`/etc/default/appctl`:

```text
$APPCTL_PROJECTS/<proyecto>/          # uno por cliente
├── compose.yaml
├── .env                              # 0600
├── nginx/site.conf                   # NO se carga hoy: ver Endurecimiento
├── db/init/                          # scripts de initdb
├── db/data/                          # volumen de la base
├── sftp/home/upload/                 # el código del cliente
├── sftp/home/private/                # solo SFTP (privado: lo crea el entrypoint)
├── backups/                          # dumps (privado: lo crea el primer db dump)
└── state.json
```

El default es `/srv/appctl`, pero lo importante es dónde: **en el mismo
filesystem que el root de Docker**. Un proyecto de cliente separado del lugar
donde viven sus datos es un restore que no cierra.

`db/data` necesita que el motor pueda escribir ahí, así que va en 755 y no en
700. Este detalle costó una tarde entera: con el volumen en 700 el contenedor
quedaba `healthy` igual, porque el healthcheck es un `pg_isready` que contesta
antes de que el motor abra sus propios datos, y los backups fallaban sin que nada
lo delatara. Por eso `create` corre una comprobación de verdad con el rol de la
app, y no un ping.

El registro de puertos va aparte, en `$APPCTL_HOME` (por defecto
`/srv/appctl/.appctl`), oculto, para que listar los proyectos muestre solo
clientes.

| Variable | Default | Para qué |
|---|---|---|
| `APPCTL_PROJECTS` | `/srv/appctl` | raíz de los proyectos (también se lee de `/etc/default/appctl`) |
| `APPCTL_HOME` | `/srv/appctl/.appctl` | registro de puertos y versiones |
| `APPCTL_HOST` | el nombre de la máquina | el host que sale en el resumen |
| `HTTPS_PROXY` | — | hace falta si la red no llega a los repos |

Sobre `APPCTL_HOST`: si no lo definís, el resumen usa el nombre de la máquina, no
`localhost`. Un nombre de host resuelve igual desde afuera si hay DNS;
`localhost` no resuelve nunca en la máquina del developer. El valor queda
congelado en `state.json` cuando creás el proyecto, así que si creaste sin
`APPCTL_HOST` y el resumen te quedó con `localhost`:

```bash
appctl <p> set-host <nombre-del-servidor>
```

`info` y `creds` avisan cuando el host guardado no sirve, con ese comando a mano.

---

## Los stacks

Cada stack es un directorio bajo `stacks/`, con su `compose.tmpl.yaml` y una
imagen propia (Dockerfile, entrypoint, nginx) donde hace falta. Agregar uno
nuevo es copiar un directorio y ajustar los placeholders.

Hay **6**, y cada uno levanta **dos contenedores**: `app` y `db`.

| Stack | App | Base | Notas |
|---|---|---|---|
| `php-postgres-sftp` | PHP 8.5 + nginx + sshd | PostgreSQL 18 | el default |
| `php-mysql-sftp` | PHP 8.5 + nginx + sshd | MariaDB 11.8 | |
| `nextjs-postgres-sftp` | Node 22 + Next.js | PostgreSQL 18 | build multi-stage, `.next` |
| `nextjs-python-postgres-sftp` | Node 22 + Next.js + API Python (FastAPI) | PostgreSQL 18 | dos runtimes en un contenedor: `/api` → uvicorn, el resto → Next |
| `express-postgres-sftp` | Node 22 + Express + React | PostgreSQL 18 | nginx sirve el build de React y proxya `/api` a Express |
| `tomcat-postgres-sftp` | Tomcat 11 + JDK 25 | PostgreSQL 18 | el proxy externo habla directo con Tomcat |

SFTP no es un contenedor aparte: es un `sshd` dentro del contenedor de la app,
con el directorio del cliente montado en los dos lados. Menos contenedores que
operar y menos superficie.

Faltan los `mysql` de Next.js y Tomcat, y un stack con `cron` para workers de
cola. El CLI ya los pide por nombre; lo que no existe todavía es el directorio.
No están built por diseño: cada uno se agrega cuando alguien lo pide de verdad,
y así el repo no carga con stacks que nadie probó.

### Next.js + API Python

`python` no es un runtime suelto: es un complemento de `nextjs`. El stack tiene
**una sola imagen** con Node y Python, un solo nginx y un solo puerto:

```text
/        -> Next standalone  (127.0.0.1:3000)
/api/    -> uvicorn + FastAPI (127.0.0.1:8000)
```

```bash
appctl acme nextjs python psql sftp
```

Por qué juntos y no dos stacks: el cliente tiene **un** proyecto, **un** puerto y
**un** SFTP. Dos contenedores serían dos puertos que abrir, dos chroots que
mantener y el código repartido en dos uploads distintos. Con `python` al lado de
`nextjs`, el developer sube todo por la misma sesión:

```text
/upload/              la app de Next (package.json, app/, etc)
/upload/backend/      la API (main.py + requirements.txt)
```

| Detalle | Cómo funciona |
|---|---|
| arranque de la API | `uvicorn main:app` (o `app.main:app` si usás un paquete `app/`), con el venv de la imagen |
| dependencias | `backend/requirements.txt`, se instalan al arrancar el contenedor |
| base de datos | la misma del proyecto, por el host `db` — no hay una segunda base |
| prefijo | `/api` **se saca** al reenviar: tu `/items` se ve en `/api/items` |
| `/healthz` | el cliente tiene que responderlo (en Next y en la API): si no, el contenedor figura `unhealthy` |

Las dependencias van a un venv (`/opt/venv`) y no al Python del sistema porque
Alpine marca el suyo como *externally managed* (PEP 668) y el `pip install` a
secas falla. `fastapi` y `uvicorn` vienen preinstalados para que el stub de
verificación ande **sin red** en un `create` recién hecho.

El healthcheck pide `/healthz` **y** `/api/healthz`. Un check que solo mira Next
deja el contenedor en verde con la API caída, que es exactamente el tipo de verde
que no sirve para nada.

### Express + React

El stack para "backend Express + frontend React", que es lo que se pide seguido.
Una sola imagen con Node 22, un solo nginx y un solo puerto:

```text
/        -> el BUILD DE REACT (estatico, con fallback SPA)
/api/    -> Express             (127.0.0.1:3000)
```

```bash
appctl acme express psql sftp
```

El cliente sube **dos carpetas** por SFTP a `/upload`:

```text
upload/backend/     package.json + server.js (el Express)
upload/frontend/    el React: la fuente, o ya buildeado en dist/ (o build/, out/)
```

El arranque resuelve los dos casos: si el backend ya tiene `node_modules` y el
frontend ya tiene un build con `index.html`, sólo arranca; si falta, instala
(`npm ci`) y buildea (`npm run build`) él mismo. El comando del backend lo decide
lo que haya: el script `start` del `package.json` manda, si no `server.js`,
`index.js` o `app.js`.

Tres decisiones que se notan en producción:

- **nginx sirve el build como estático y proxya `/api`** al backend sin comerse
  el prefijo: las rutas del cliente (`app.get('/api/x')`) llegan completas.
- **Fallback SPA sólo para las rutas**: `/clientes/7` devuelve el `index.html`
  (React Router resuelve eso en el cliente), pero un `.js` que no existe devuelve
  **404** y no el HTML — si no, el navegador recibe HTML donde espera un módulo y
  el error visible es `Unexpected token '<'`, que no dice nada de la causa.
- **El healthcheck mira nginx** (que conteste cualquier 1xx-4xx), no un `/healthz`
  de la app: atado al archivo de la app, cualquier cliente cuyo código no lo
  tenga queda `unhealthy` para siempre en cuanto sube su app de verdad.

El stub de un proyecto recién creado **es Express de verdad** (instalado en la
imagen, no en el arranque) y contesta `/api/healthz` con la versión de Express,
la de node y el estado de la base: verificar un stack nuevo tiene que probar el
stack, no un placeholder de texto.


Si la red es cerrada, el `create` guarda `HTTP_PROXY`/`HTTPS_PROXY` en el `.env`
del proyecto cuando los tenés en el entorno (`appctl` no inventa un proxy): el
`npm ci` corre en el **arranque del contenedor**, así que el proxy tiene que
estar ahí y no sólo en la máquina donde se construye la imagen. Sin eso, un
proyecto con código real no arranca y lo único visible es `npm ERR! network`.

### Tomcat

Tomcat cambia la forma del stack porque **no es PHP**: no hay `.php` que servir,
hay un `.war` que compilar y deployar.

| | `php` | `tomcat` |
|---|---|---|
| código | el cliente lo sube y se sirve tal cual | `.war` compilado con `mvn package` o `gradle build` |
| dónde vive | `sftp/home/upload/` | `webapps/<proyecto>.war` en el host |
| camino | nginx → `php-fpm:9000` | el proxy externo → `tomcat:8080` |

No hay nginx adentro: el proxy externo habla directo con Tomcat. Es lo más
simple que funciona, y ya que el proxy va adelante para el TLS, meter un nginx
adentro sería un salto extra para no ganar nada.

Imagen: `tomcat:11-jdk25-temurin-noble` (Tomcat 11 = Jakarta EE 10, requiere Java
17 o superior). **Ojo:** Tomcat 10 pasó a Jakarta y **no** es compatible
binariamente con Tomcat 9 (`javax.*` → `jakarta.*`). Un `.war` que hoy compila en
9 no corre en 11 sin tocar imports. Hay que preguntar la versión al cliente en
lugar de asumir la última.

### MySQL

`mysql` es alternativa a `psql`, nunca adicional. Con PHP lo normal es
**MariaDB 11.8**: GPL, sin fricción de licencia, mejor embed. MySQL 8.4 (Oracle)
solo si el cliente lo pide explícitamente. Con Tomcat o Next.js, MySQL 8.4 sí
tiene más sentido.

---

## Los componentes

| Componente | Qué agrega |
|---|---|
| `php` | PHP-FPM 8.5 + nginx + sshd, volumen de código |
| `nextjs` | Node 22 + build de Next.js, volumen de app |
| `python` | API FastAPI + uvicorn al lado de Next (complemento: solo con `nextjs`) |
| `tomcat` | Tomcat 11 + JDK 25, el proxy externo habla directo |
| `psql` / `mysql` | PostgreSQL 18 / MariaDB 11.8, red privada |
| `sftp` | sshd con chroot, puerto dedicado, volumen compartido con la app |
| `cron` | crontab del cliente, ejecutado dentro del contenedor de la app |

`cron` es una bandera, no un stack: no cambia la imagen ni la base, solo hace que
el entrypoint habilite el crontab que el cliente sube a `/upload/cron/crontab`.
Está porque la mayoría de los pedidos de PHP con Laravel o WordPress termina
necesitando un worker, y sin esto el primer "necesito correr un cron" es un
ticket de soporte. Requiere `sftp`, que es por donde el cliente sube el archivo.

`python` es la excepción simétrica: tampoco es un runtime suelto (no existe un
stack solo de Python con base y SFTP), pero **sí** cambia la imagen — el stack
`nextjs-python-postgres-sftp` trae Node y Python. Por eso entra en el nombre del
stack y no en la lista de runtimes: `appctl p python psql sftp` se rechaza con un
mensaje que dice qué usar, en vez de fallar más tarde con un stack inexistente.

```bash
appctl acme php psql sftp
appctl acme php mysql
appctl acme nextjs psql sftp --app-port 3001
appctl acme nextjs python psql sftp
appctl acme php psql sftp cron --app-memory 2G
```

**Opciones de `create`:**

| Opción | Default | Qué hace |
|---|---|---|
| `--app-port N` | primer libre desde 8001 | puerto HTTP del proyecto |
| `--sftp-port N` | 2221 | puerto SFTP del proyecto |
| `--php VER` | tag por defecto | fija la versión de PHP, de 8.1 a 8.9 |
| `--app-memory M` | 512M (1G en Tomcat) | techo de RAM del contenedor de app |
| `--db-memory M` | 512M | techo de RAM del contenedor de base |
| `--host NAME` | `$APPCTL_HOST` | host que aparece en el resumen |
| `--dry-run` | — | muestra lo que haría, sin tocar nada |

**Invariantes que valida antes de crear:**

- `proyecto` es `[a-z0-9][a-z0-9-]{1,30}[a-z0-9]`
- hay al menos un runtime (`php`, `nextjs` o `tomcat`) y al menos un servicio
  (`psql`, `mysql` o `sftp`)
- `psql` y `mysql` son alternativas, nunca acumulables
- el puerto de app y el de SFTP están libres, en el registro y en la red real
- el proyecto no existe todavía
- el stack resultante existe en `stacks/`

---

## El registry: qué versión se instala

Los tags se resuelven en `create`, no están escritos en el código:

```bash
appctl acme php psql sftp            # PHP 8.5 + PostgreSQL 18
appctl acme php psql sftp --php 8.3  # fijado a 8.3
```

El tag flotante vive en `$APPCTL_HOME/versions.json`, con `flock` de escritura.
La regla es que **el tag exacto se resuelve una vez y se escribe en el compose y
en `state.json`**. Un contenedor que corre no se actualiza solo, nunca:
actualizar es `appctl <p> upgrade`, y es explícito a propósito.

La razón es concreta. `restart: unless-stopped` no re-pullea, pero un `docker
compose pull` dentro de un `up` futuro sí lo haría. Si el tag flotara, el
cliente A sube a PHP 8.6 un martes cualquiera porque alguien corrió `up`, sin
haberlo decidido. Fijado en el YAML, eso no puede pasar.

---

## Qué genera y con qué permisos

| Objeto | Nombre | Permisos |
|---|---|---|
| Base de datos | `<proyecto>_db` | — |
| Rol de la app | `<proyecto>` | `SELECT`, `INSERT`, `UPDATE`, `DELETE` y `CREATE TEMPORARY TABLE` |
| Rol de migraciones | `<proyecto>_mig` | todo, incluido `CREATE SCHEMA` y `DROP` |
| Rol de lectura | `<proyecto>_ro` | `SELECT` |
| Superusuario del motor | `<proyecto>_admin` | nunca aparece en el resumen |
| Usuario SFTP | `<proyecto>` | chroot a su home, `upload/` escribible |
| `.env` | 0600 | secretos en claro, nunca en el `compose.yaml` |

En PostgreSQL, además de `CREATE ROLE` y `CREATE DATABASE OWNER`, el init corre
`REVOKE CONNECT ON DATABASE ... FROM PUBLIC`: sin eso, cualquier rol del cluster
se puede conectar a la base de un cliente. En MariaDB no hay roles: el init crea
los mismos tres **usuarios**, con host `'%'` (sin host solo entrarían por socket
local) y con los privilegios acotados a la base del proyecto, nunca a `*.*`.

Las tablas **temporales** son el caso que ese `REVOKE` se lleva puesto: en
Postgres el privilegio `TEMP` sobre la base viene otorgado a `PUBLIC` por
defecto, y revocárselo a `PUBLIC` (que es lo correcto) también se lo saca a los
roles del proyecto. El init lo vuelve a otorgar explícitamente al rol de la app y
al de migraciones: una tabla temporal vive en el esquema temporal de la sesión,
no la ve nadie más, muere al cerrar la conexión y no habilita ninguna escritura
sobre datos persistentes. El rol de **solo lectura no lo lleva** — solo lectura
es solo lectura, y hay un gate que lo verifica.

### Por qué los roles están separados

El rol de la app no necesita `CREATE SCHEMA` ni `DROP`, pero `artisan migrate`
sí. Si fueran el mismo, un SQL injection en la aplicación podría borrar el
schema completo. Son dos roles con dos passwords distintas, y el developer
recibe el de migraciones aparte, con la instrucción de usarlo solo para migrar.

Las passwords las genera `lib/gen.py` con un alfabeto reducido a propósito:
letras, dígitos y símbolos que no rompen nada. Nada de comillas, backslashes,
`$`, `;` ni espacios, porque una password así rompe el `.env`, un comando de
shell, una URL de conexión, un `.pgpass` o un string JDBC. Un cliente que copia
y pega y falla a las dos de la mañana es un ticket que preferimos no tener.

---

## El resumen que recibe el developer

`create` imprime una sola vez un resumen en texto plano, pensado para reenviar
sin editarlo. En el stack `nextjs-python` el resumen agrega el bloque de la API
(URL en `/api/`, dónde va el backend y qué tiene que responder):

```text
API Python  (FastAPI + uvicorn, detras de nginx)
  URL        http://apps.example.com:8001/api/
  Codigo     $APPCTL_PROJECTS/acme/sftp/home/upload/backend   (sube por SFTP)
```

```text
acme  (php-postgres-sftp)
--------------------------------------------------------------
Acceso a la aplicacion
  URL        http://apps.example.com:8001
  Codigo     $APPCTL_PROJECTS/acme/sftp/home/upload   (sube por SFTP)

Acceso por SFTP
  Host       apps.example.com
  Puerto     2221
  Usuario    acme
  Password   ...
  Carpeta    /upload/            (dentro del chroot)
  Comando    sftp -P 2221 acme@apps.example.com

Base de datos  (solo accesible desde la app)
  DB        acme_db

  Usuario de la aplicacion
    User     acme
    Pass     ...
    Permisos SELECT / INSERT / UPDATE / DELETE
    (sin CREATE ni DROP: no puede borrar el schema)

  Usuario de migraciones  (para artisan migrate, etc)
    User     acme_mig
    Pass     ...
    Permisos ALL sobre la base

  Usuario de solo lectura  (reportes, backups)
    User     acme_ro
    Pass     ...
    Permisos SELECT

--------------------------------------------------------------
Credenciales en  $APPCTL_PROJECTS/acme/.env   (0600)
Gestion         appctl acme creds
Rotar           appctl acme rotate sftp
```

Después las credenciales se vuelven a leer con `appctl <p> creds`, que las
imprime en pantalla y quedan en el scrollback: conviene tenerlo en cuenta.

El resumen usa el nombre del host, no la IP. El developer lo copia y funciona; si
mañana el servidor cambia de dirección, el resumen no quedó viejo.

---

## La base de datos

La base **nunca se expone sola**. Vive en una red `internal: true` sin puerto
publicado, y `create` lo verifica de verdad: lo prueba desde otro contenedor del
proyecto y desde el host, no leyendo la config.

### Las dos redes

Cada proyecto tiene dos, y que sean dos es el punto:

```yaml
networks:
  # con salida a internet: la app llama a una pasarela de pagos, a un SMTP,
  # a la API de un whatsapp. Sin esto el cliente no puede cobrar.
  egress:
    driver: bridge
  # sin gateway. La garantía de que la base no se ve desde afuera, hecha con
  # machinery y no con "no le puse ports:".
  data:
    driver: bridge
    internal: true
```

`app` está en las dos: sale a internet para `composer` y `packagist`, y llega a
la base solo por `data`. `db` está solo en `data`.

Poner `internal: true` en la red de todo el stack rompería las apps, que
necesitan salir. Y si las dos redes fueran una, la base sería alcanzable desde
cualquier cosa que pueda enrutar al host.

### Backups, export e import

```bash
appctl <p> db dump                 # a <p>/backups/, con fecha, gzip y sha256
appctl <p> db dump -o /tmp/x.dump.gz   # a donde le digas
appctl <p> db list                 # qué backups hay
appctl <p> db restore <archivo> --yes
appctl <p> db rm <archivo> --yes   # borrar uno (no se puede deshacer)
```

El dump sale **de adentro del contenedor**, se guarda comprimido con gzip y lleva
`.dump.gz`: no hace falta `psql` en el host, y la base no necesita ser accesible.
Corre en los dos motores con el mismo comando. Cada dump deja un archivo gzip
con la fecha en el nombre y un `.json` al lado con el motor, el tamaño comprimido
y el sha256.

`restore` **pisa la base**, así que avisá que está a punto de pasar y guarda el
estado anterior con el comando exacto para volver atrás.

**`restore` sin `--clean`**, y es a propósito: un `--clean --if-exists` genera
`DROP ROLE` del origen y el restore de un clon muere con
`FATAL: role "x" does not exist`. Solo usá `--clean` cuando el destino ya tiene
objetos del mismo nombre.

Los backups van a `<proyecto>/backups/`, que no se commitea: `.env` y `*.sql`
están en `.gitignore`. Un dump tiene datos de clientes.

### Exponerla

```bash
appctl <p> db expose 5432                     # publica en 0.0.0.0 (cualquier red que llegue)
appctl <p> db expose 5432 --bind 127.0.0.1    # solo este host (para un tunel ssh)
appctl <p> db expose 5432 --bind 10.20.0.5    # desde una IP concreta del host
appctl <p> db unexpose                        # cerrarla
```

El default es `0.0.0.0`: si alguien expone la base es para que un cliente entre
desde otra máquina, y publicar solo en `127.0.0.1` obliga a un túnel `ssh -L`.
El comando lo dice al publicar, con la dirección útil (`host:puerto`) y cómo
acotarlo. La base igual queda protegida: sigue pidiendo password, sigue en su
red interna y `db grant --cidr` limita desde qué red se conecta cada usuario.
`--bind` acepta una IP y no una red, porque docker rechaza un CIDR en `ports:`;
`--bind 127.0.0.1` es para el caso "solo por túnel".

El puerto y el bind quedan anotados en `state.json`: `appctl <p> info` los
muestra y un `upgrade` no los pierde. Antes vivían solo en el `compose.yaml`
renderizado, así que el próximo re-render los borraba en silencio y la base
quedaba cerrada con el cliente creyendo que seguía abierta.

`info` y `creds` muestran la dirección con la que se conecta un cliente, no el
bind crudo. Con `0.0.0.0` (el default) muestran el **host** (`dicappsrv…:5435`)
y aclaran que entra cualquiera que llegue a esa dirección; con `--bind 127.0.0.1`
muestran `127.0.0.1:5435` y avisan que **de afuera no entra** — el
nombre del host ahí sería una dirección que no atiende a nadie. `db expose` da
ese aviso al publicar, y `creds` (el resumen que recibe el developer) incluye el
estado de la base: antes decía siempre "la DB no se abre desde internet", que con
un puerto expuesto era falso.

### Usuarios de la base

```bash
appctl <p> db users                              # qué hay y con qué permisos
appctl <p> db grant consulting --rol readonly     # readonly, write o migrate
appctl <p> db grant dev --rol write --cidr 10.20.0.0/16
appctl <p> db revoke consulting
```

`grant` crea el usuario con password propia y el rol pedido.

En MariaDB el usuario de migración necesita `WITH GRANT OPTION` sobre la base
**y** `CREATE USER` global. MariaDB rechaza `GRANT OPTION ON db.*` con 1064
porque no es un privilegio, es una opción de `GRANT`; y con solo `WITH GRANT
OPTION` da `1227: you need (at least one of) the CREATE USER privilege`. El
alcance global no molesta acá porque cada base está en su red y no comparte nada
con otros clientes: el aislamiento lo da la red. En un MySQL con varias bases en
el mismo servidor, sí.

---

## Comandos

### Crear, listar y diagnosticar

| Comando | Qué hace |
|---|---|
| `appctl <proyecto> <componentes...>` | crea un proyecto |
| `appctl create <proyecto> <componentes...>` | igual, explícito |
| `appctl build [--runtime X] [--stack S] [--force]` | construye las imágenes base; se corre una vez |
| `appctl list` | tabla de proyectos: nombre, stack, puertos y base |
| `appctl ps` | los stacks de appctl con sus puertos y estado |
| `appctl doctor` | estado del host: Docker, registry, proxy, `sudo -n`, `sshpass` |
| `appctl <proyecto> info` | detalle del proyecto, sus imágenes, sus servicios y sus límites de RAM/CPU |
| `appctl <proyecto> set-host <nombre>` | corrige el host del resumen |

### Operar un proyecto

| Comando | Qué hace |
|---|---|
| `appctl <proyecto> creds` | vuelve a mostrar el `.env` |
| `appctl <proyecto> logs [-f] [--service app\|db]` | logs, opcionalmente en vivo |
| `appctl <proyecto> shell [--service app\|db]` | shell dentro de un contenedor |
| `appctl <proyecto> psql [-U usuario] [-d base]` | `psql` ya conectado, sin pedir password |
| `appctl <proyecto> restart` | reinicia los servicios |
| `appctl <proyecto> rotate <sftp\|db\|mig\|ro>` | cambia una credencial |
| `appctl <proyecto> limits [--service app\|db] [--memory M] [--db-memory M] [--cpus N] [--save]` | ajusta RAM y CPU en caliente, por servicio o los dos |
| `appctl <proyecto> upgrade [-y] [--php VER] [--runtime R]` | re-aplica el compose del template (acepta que ya haya codigo real: pide HTTP 200, no el stub) |
| `appctl clone <origen> <nuevo> [--port N] [--php VER]` | copia un proyecto con sus datos |
| `appctl <proyecto> destroy [--keep-data] [--yes]` | destruye el proyecto |

### Base de datos

El proyecto va antes del subcomando:

```bash
appctl <proyecto> db dump [-o ruta]           # exporta la base en gzip
appctl <proyecto> db restore <archivo> --yes # importa; PISA la base
appctl <proyecto> db list                    # backups que hay
appctl <proyecto> db rm <archivo> -y         # borra un backup
appctl <proyecto> db expose <puerto> [--bind IP]  # publica (default 0.0.0.0)
appctl <proyecto> db unexpose                # cierra
appctl <proyecto> db grant <usuario>         # alta de usuario externo
appctl <proyecto> db revoke <usuario> --yes  # le saca el acceso
appctl <proyecto> db users                   # usuarios de la base y sus permisos
```

---

## Versiones verificadas

Bajadas una por una contra Docker Hub, no de memoria (2026-10-01):

| Imagen | Tag | Tamaño |
|---|---|---|
| PHP | `php:8.5-fpm-alpine` | **150 MB** ← el que usa el stack |
| PHP | `php:8.5-fpm-bookworm` | 732 MB |
| PostgreSQL | `postgres:18-alpine` | 433 MB |
| MariaDB | `mariadb:11.8` | 458 MB |
| Node | `node:22-alpine` | 238 MB |
| nginx | `nginx:stable-alpine` | 93.6 MB |
| Tomcat | `tomcat:11-jdk25-temurin-noble` | 602 MB |
| Tomcat | `tomcat:10.1-jdk21-temurin-noble` | 729 MB |
| Tomcat | `tomcat:9-jdk21-temurin-noble` | 730 MB |

`php:8.5-fpm-alpine` pesa 150 MB contra 732 MB de bookworm, cinco veces menos,
y eso decide cuántos clientes entran en el disco. El stack de PHP usa alpine.
Las extensiones que un cliente pide seguido (`intl`, `gd`, `zip`, `bcmath`,
`soap`) se compilan ahí sin problema y vienen en la imagen: no hace falta caer a
bookworm por eso. Ver *Qué trae la imagen PHP*.

En Tomcat, los tags con `jre` en el nombre no existen: solo están los `jdk`.

---

## Endurecimiento de PHP y nginx

### Qué trae la imagen PHP

Medido dentro del contenedor con `php -m` y `extension_loaded()`, no copiado de
una tabla de paquetes Debian (los nombres `php8.5-*` son de Debian; acá son las
extensiones):

| Extensión | Para qué la pide un cliente |
|---|---|
| `pdo_pgsql` + `pgsql` | conectarse a PostgreSQL (o `pdo_mysql` + `mysqli` en el stack de MySQL) |
| `mbstring` | strings UTF-8 (tildes, ñ) |
| `intl` | fechas, números y monedas por ICU; Laravel y Symfony lo usan |
| `gd` | imágenes: TCPDF, PhpSpreadsheet, thumbnails |
| `zip` | `ZipArchive`: exports de Excel (Laravel Excel, zipstream) |
| `bcmath` | decimales exactos: JWK/JWT, barcodes PDF417 de TCPDF |
| `soap` | clientes SOAP legacy |
| `xml`, `dom`, `SimpleXML`, `xmlreader`, `xmlwriter`, `libxml` | parseo y generación de XML |
| `curl` | HTTP saliente (`wp_remote_get`, Guzzle) |
| `Zend OPcache` | caché de opcodes del runtime |

Dos detalles que se pagan solos:

- `Zend OPcache` viene **compilado** en la imagen oficial, sin `.so` propio: se
  consulta con ese nombre exacto (`extension_loaded('Zend OPcache')`), y buscar
  `opcache` a secas da falso. No está en el `php.ini` porque no hace falta.
- `intl` necesita los datos de ICU, y alpine los parte: `icu-data-en` trae solo
  los locales ingleses. Con eso, `IntlDateFormatter('es_AR')` **formatea en
  inglés** sin avisar. La imagen instala `icu-data-full` (906 locales, `es_AR`
  incluido).

Si un cliente necesita una extensión que no está en la lista, se agrega al
Dockerfile del stack (`stacks/php-*-sftp/image/Dockerfile`), se reconstruye la
imagen y se mueve el proyecto con `upgrade`. Ojo con las dependencias de build:
van en un grupo `apk --virtual` que se borra al final, y las librerías de *runtime*
tienen que quedar afuera de ese grupo porque `apk del` se lleva las del grupo.


Los stacks de PHP salen con esto puesto de fábrica. No hay que tocar nada por
proyecto: va horneado en la imagen.

### Sesión y cookies

| Directiva | Valor | Qué corta |
|---|---|---|
| `session.use_strict_mode` | `1` | un id de sesión inventado por el cliente (session fixation) |
| `session.use_only_cookies` | `1` | que la sesión viaje por la URL |
| `session.cookie_httponly` | `1` | que JavaScript lea la cookie (robo por XSS) |
| `session.cookie_secure` | `1` | que la cookie viaje por HTTP sin TLS |
| `session.cookie_samesite` | `Lax` | el envío de la cookie en pedidos cross-site |

`cookie_secure=1` convive con el TLS terminado en el proxy de adelante: el que
decide si manda la cookie es el navegador, y ve HTTPS aunque el backend hable
HTTP.

**Si la app se usa por HTTP plano** (sin nada que termine TLS), esa cookie no
viaja y el login no queda logueado nunca. Se ve como "la app no guarda la
sesión", y no es la app. Se arregla por proyecto:

```bash
echo 'APPCTL_SESSION_COOKIE_SECURE=0' >> $APPCTL_PROJECTS/acme/.env
appctl acme upgrade --yes
```

### Wrappers remotos

| Directiva | Valor | Qué corta |
|---|---|---|
| `allow_url_fopen` | `Off` | `file_get_contents('http://...')`: la vía clásica de RFI y SSRF |
| `allow_url_include` | `Off` | que un `include` traiga código de una URL |
| `cgi.fix_pathinfo` | `0` | que `/upload/foto.jpg/x.php` ejecute el `.jpg` |

Composer, Guzzle y `wp_remote_get` usan la extensión `curl` (está en la
imagen), no `fopen`: no se rompen. Si igual necesitás `fopen` sobre URLs:

```bash
echo 'APPCTL_ALLOW_URL_FOPEN=On' >> $APPCTL_PROJECTS/acme/.env
appctl acme upgrade --yes
```

### Listado de directorios, y el `.htaccess` que no se lee

**nginx ignora los `.htaccess` por completo.** Las directivas de Apache —el
`Options -Indexes`, un `Deny from all`, un `RewriteRule`— no se leen en este
stack. Son archivos que no hacen nada. Si la app depende de un `.htaccess` para
algo, ese algo no está pasando, y conviene saberlo antes de confiar en él.

El listado de directorios igual está cerrado, porque nginx **no lista por
defecto** (Apache sí, y por eso necesita `Options -Indexes`). Un pedido a un
directorio sin `index` responde `403`. El vhost deja el `autoindex off`
explícito y agrega las cabeceras:

| Directiva | Valor |
|---|---|
| `autoindex` | `off` |
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `SAMEORIGIN` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |

HSTS no va acá: el TLS lo termina el proxy de adelante, y un
`Strict-Transport-Security` emitido por un backend que habla HTTP no llega al
navegador. Lo tiene que emitir el proxy.

Los `.htaccess`, los `.env` y todo lo que empieza con punto tampoco se sirven:
el vhost los niega.

### Dónde vive esta config

El vhost de nginx está **horneado en la imagen** (`image/nginx.conf`). Cambiarlo
es `appctl build` y después `appctl <proyecto> upgrade`. El `nginx/site.conf`
que el proyecto tiene en su directorio **hoy no se carga**: nginx no incluye
`conf.d/`, así que editarlo no tiene efecto. Está anotado como pendiente.

### Directorio privado (solo SFTP)

Cada proyecto tiene un `sftp/home/private/` hermano de `upload/`: el cliente lo
ve por SFTP como `/private` y guarda ahí lo que no debe salir por web. En los
stacks PHP nginx tiene el root en `/upload`, así que no lo sirve; además hay un
`location` que lo niega explícito y `disable_symlinks on`, que frena el truco del
symlink `upload/link -> ../private`. En `nextjs-python` nginx es proxy puro (no
hay root), y el `deny` queda igual como defensa en profundidad: el día que
alguien agregue un server block con root, la barrera ya está puesta. Queda con
dueño el usuario SFTP y modo 700: ni nginx ni php-fpm entran. Desde PHP se llega
por filesystem con la ruta `/srv/sftp/private`, si los permisos lo permiten.

Por SFTP el cliente ve solo `/upload` y `/private`, nada más. El SFTP corre con `ForceCommand
internal-sftp` dentro del propio sshd, así que el chroot no lleva binarios:
nada de `bin/`, `etc/`, `lib/` ni `usr/` a la vista. `/tmp` no se crea más
(era pasajero); en proyectos viejos se quita solo si está vacío — si el
cliente dejó archivos ahí, se queda. En proyectos creados
antes de este cambio, el entrypoint borra esos directorios de andamiaje al
arrancar (lista explícita, nunca los del cliente).

El entrypoint lo crea con `mkdir -p` y nunca con `rm -rf`: un borrado ahí
pegaría en el disco real del host a través del bind mount. Esa fue la causa
de un bug donde cada reinicio pelaba el `upload` del cliente.

---

## Qué verifica `create`

Después de levantar el stack, `create` corre diez comprobaciones y reporta
**todas**, no solo la primera que falla: cuando algo anda mal a las tres de la
mañana, lo que se quiere es la lista completa de una vez. En el stack
`nextjs-python` son **once**: la API se verifica aparte (ver el punto 9).

1. Los contenedores `db` y `app` están `healthy`.
2. Los tres roles del proyecto existen.
3. La base **rechaza** conexiones sin password, o sea que el `pg_hba` no quedó en
   `trust`.
4. La base se puede usar: una consulta real con el rol de la app, no un ping.
5. La base responde desde la red interna.
6. La base **no es alcanzable desde el host**.
7. La base **no es alcanzable desde la red de otro proyecto**, apuntando a su IP
   real. El nombre `db` resuelve a otra base y daría un falso positivo.
8. La app responde lo que tiene que responder, según el runtime del stack.
9. La API responde en `/api/` (solo en `nextjs-python`). Es un check aparte
   porque nginx sirve dos runtimes en el mismo puerto: un 200 en `/` lo contesta
   Next, así que no prueba nada de Python. Con uvicorn caído, `/` sigue dando 200
   y el proyecto se vería sano con la API tirada.
10. El login SFTP funciona: entra, lista y **escribe** un archivo. Un SFTP de solo
    lectura no sirve para subir código, y un `ls` exitoso no lo detecta.
11. El estado del compose no tiene drift.

Los puntos 6 y 7 son los que importan. Que un puerto no esté en el
`compose.yaml` no es una garantía: es una ausencia de configuración, y la
configuración la edita cualquiera. Lo que se comprueba es el comportamiento. Si
alguna vez una imagen de base de datos trajera un `ports:` propio, la creación
se detendría ahí y no tres meses después.

---

## Idempotencia y concurrencia

- `flock` sobre `registry.json` durante todo `create` y `destroy`. Sin eso, dos
  altas en paralelo leen el mismo puerto libre, las dos lo reservan, y una
  termina con un contenedor que falla y un registro que miente.
- Los dos puertos (app y SFTP) se reservan en el registro **antes** de levantar
  nada.
- Además del registro se consulta `ss -ltnp`: el registro puede mentir si
  alguien levantó un contenedor a mano, y se le cree a la red, no al archivo.
- Los puertos de servicios del sistema (22, 80, 443, 3306, 5432, 8080) nunca se
  asignan, aunque estén libres.
- `create` sobre un proyecto existente falla. `destroy` exige `--yes` o
  confirmación interactiva.

---

## Disco: el límite real antes que la RAM

Un stack `php + postgres` ronda 800 MB de imagen. Con datos, logs y backups, cada
cliente se va a 1.5-2 GB en la práctica. Eso da unos 15 clientes con margen en un
volumen de 32 GB, no los 30 que salen de la cuenta optimista solo con la RAM.

Cuando se llegue a 10 clientes, medir el uso real y recién ahí decidir: agrandar
el volumen, o mover los datos afuera. Antes no.

---

## Estructura del repo

```text
appctl/
├── bin/appctl                 # el CLI, stdlib y nada más
├── lib/
│   ├── gen.py                 # renderiza el compose y el .env
│   ├── ports.py               # registro de puertos + flock
│   ├── dbtool.py              # dump / restore / expose / grant
│   ├── smoke.py               # las comprobaciones (11 en nextjs-python)
│   └── summary.py             # el resumen para el developer
├── stacks/                    # 6 stacks, uno por directorio
└── tests/
    ├── test_init_sql.py       # ejecuta los init y valida el SQL que producen
    ├── test_root_pw.py        # parsea la línea del log de mariadb
    ├── test_security.py       # el compose no publica la DB
    ├── test_php_hardening.py  # cookies, headers y el .htaccess que no se lee
    ├── test_private_sftp.py   # el dir privado: 700, deny y sin rm -rf
    ├── test_sftp_chroot.py    # el chroot solo trae upload y private
    ├── test_nextjs_python.py  # el stack de dos runtimes: nginx, uvicorn, chroot
    ├── test_app_check_status.py     # el puerto publicado: state, info y el bind
    ├── test_db_publish.py     # el puerto publicado: state, info y el bind
    ├── test_readme.py         # cada afirmación del README contra el código
    └── check_names.py         # AST: llamadas sin definir, defs duplicadas
```

Dos tests de estos se escribieron porque el software te estaba mintiendo:

`test_init_sql.py` corre los init de verdad, con `psql` y `mariadb` sustituidos
por un binario que hace `cat`, y mira el SQL que llega. Un init corre una sola
vez, cuando se crea el volumen: si falla ahí no hay reintento, y el proyecto
queda con una base sin sus tres usuarios para siempre.

`test_readme.py` extrae cada afirmación verificable de este README (los stacks,
los flags, los subcomandos, las versiones, los defaults, las redes) y la
contrasta contra los archivos. Existió porque el README mentía: decía que había
8 stacks y había 4, listaba dos módulos de `lib/` que nunca se escribieron, y
documentaba un flag que el parser nunca definía. Nada de eso se ve leyendo el
documento; se ve corriendo el test.

---

## Estado

Funcional y verificado de punta a punta con PostgreSQL y MariaDB:

| | |
|---|---|
| `create` | 10/10 comprobaciones: contenedores healthy, base respondiendo, roles creados, password exigida, base inalcanzable desde el host y desde otro proyecto |
| `clone` | datos, índices y credenciales nuevas; el clon es independiente del origen |
| `upgrade` | PHP 8.5 → 8.4 → 8.5, con los datos intactos |
| `db dump` / `restore` | PostgreSQL y MariaDB |
| `db expose` | publica la base en un puerto del host, por defecto en `0.0.0.0` (cualquier red que llegue; `--bind 127.0.0.1` para solo-este-host); el puerto queda anotado y un `upgrade` no lo pierde |
| `db grant` | alta de usuarios externos, cada uno con su password |

Los cuatro stacks fueron verificados de punta a punta, incluido
`tomcat-postgres-sftp`.