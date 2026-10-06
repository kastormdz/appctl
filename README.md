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
├── nginx/site.conf
├── db/init/                          # scripts de initdb
├── db/data/                          # volumen de la base
├── sftp/home/upload/                 # el código del cliente
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

Hay **4**, y cada uno levanta **dos contenedores**: `app` y `db`.

| Stack | App | Base | Notas |
|---|---|---|---|
| `php-postgres-sftp` | PHP 8.5 + nginx + sshd | PostgreSQL 18 | el default |
| `php-mysql-sftp` | PHP 8.5 + nginx + sshd | MariaDB 11.8 | |
| `nextjs-postgres-sftp` | Node 22 + Next.js | PostgreSQL 18 | build multi-stage, `.next` |
| `tomcat-postgres-sftp` | Tomcat 11 + JDK 25 | PostgreSQL 18 | el proxy externo habla directo con Tomcat |

SFTP no es un contenedor aparte: es un `sshd` dentro del contenedor de la app,
con el directorio del cliente montado en los dos lados. Menos contenedores que
operar y menos superficie.

Faltan los `mysql` de Next.js y Tomcat, y un stack con `cron` para workers de
cola. El CLI ya los pide por nombre; lo que no existe todavía es el directorio.
No están built por diseño: cada uno se agrega cuando alguien lo pide de verdad,
y así el repo no carga con stacks que nadie probó.

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
| `tomcat` | Tomcat 11 + JDK 25, el proxy externo habla directo |
| `psql` / `mysql` | PostgreSQL 18 / MariaDB 11.8, red privada |
| `sftp` | sshd con chroot, puerto dedicado, volumen compartido con la app |
| `cron` | crontab del cliente, ejecutado dentro del contenedor de la app |

`cron` es una bandera, no un stack: no cambia la imagen ni la base, solo hace que
el entrypoint habilite el crontab que el cliente sube a `/upload/cron/crontab`.
Está porque la mayoría de los pedidos de PHP con Laravel o WordPress termina
necesitando un worker, y sin esto el primer "necesito correr un cron" es un
ticket de soporte. Requiere `sftp`, que es por donde el cliente sube el archivo.

```bash
appctl acme php psql sftp
appctl acme php mysql
appctl acme nextjs psql sftp --app-port 3001
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
| Rol de la app | `<proyecto>` | `SELECT`, `INSERT`, `UPDATE`, `DELETE` |
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
sin editarlo:

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
appctl <p> db expose 5432                     # solo desde 127.0.0.1
appctl <p> db expose 5432 --cidr 10.20.0.0/16  # abrir a una red
appctl <p> db unexpose                        # cerrarla
```

El default es `127.0.0.1` y no una red. Con un `/8` cualquiera, cualquier LAN que
llegue al puerto entra, y quien llama al comando no tiene por qué saber que la
base quedó abierta a medio entorno. Publicar el puerto no saca la base de su red
interna: es una publicación del host, no un permiso.

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
| `appctl <proyecto> info` | detalle del proyecto, sus imágenes y sus servicios |
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
| `appctl <proyecto> limits [--memory M] [--cpus N] [--save]` | ajusta RAM y CPU en caliente |
| `appctl <proyecto> upgrade [-y] [--php VER] [--runtime R]` | re-aplica el compose del template |
| `appctl clone <origen> <nuevo> [--port N] [--php VER]` | copia un proyecto con sus datos |
| `appctl <proyecto> destroy [--keep-data] [--yes]` | destruye el proyecto |

### Base de datos

El proyecto va antes del subcomando:

```bash
appctl <proyecto> db dump [-o ruta]           # exporta la base en gzip
appctl <proyecto> db restore <archivo> --yes # importa; PISA la base
appctl <proyecto> db list                    # backups que hay
appctl <proyecto> db rm <archivo> -y         # borra un backup
appctl <proyecto> db expose <puerto>         # publica (default 127.0.0.1)
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
y eso decide cuántos clientes entran en el disco. El stack de PHP usa alpine; si
un cliente necesita una extensión que no compila ahí (GD con librerías del
sistema, por ejemplo), se cae a bookworm y se acepta el costo.

En Tomcat, los tags con `jre` en el nombre no existen: solo están los `jdk`.

---

## Qué verifica `create`

Después de levantar el stack, `create` corre diez comprobaciones y reporta
**todas**, no solo la primera que falla: cuando algo anda mal a las tres de la
mañana, lo que se quiere es la lista completa de una vez.

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
9. El login SFTP funciona: entra, lista y **escribe** un archivo. Un SFTP de solo
   lectura no sirve para subir código, y un `ls` exitoso no lo detecta.
10. El estado del compose no tiene drift.

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
│   ├── smoke.py               # las diez comprobaciones
│   └── summary.py             # el resumen para el developer
├── stacks/                    # 4 stacks, uno por directorio
└── tests/
    ├── test_init_sql.py       # ejecuta los init y valida el SQL que producen
    ├── test_root_pw.py        # parsea la línea del log de mariadb
    ├── test_security.py       # el compose no publica la DB
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
| `db expose` | publica la base en un puerto del host; por defecto solo desde `127.0.0.1` |
| `db grant` | alta de usuarios externos, cada uno con su password |

Los cuatro stacks fueron verificados de punta a punta, incluido
`tomcat-postgres-sftp`.