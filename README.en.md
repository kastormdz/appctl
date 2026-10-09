# appctl

```bash
appctl acme php psql sftp
```

That brings up nginx, PHP 8.5, PostgreSQL 18 and SFTP, publishes only the two
ports that are actually needed, leaves the database unreachable from outside,
and prints the summary with everything the developer needs.

[Español](README.md)

---

## What this is

A provisioner. You tell it what the client needs and it hands back a running
stack that goes away with a single command.

It is not a PaaS. There's no web panel, no deploys by `git push`, no billing, no
multi-server. If that's what you're after, Coolify and Dokploy already do it, and
you'd be the one operating and updating them. The problem this repo solves is a
different one: a client asks for PHP with PostgreSQL and SFTP, and that exists
today instead of in three days.

Each client is a directory with a `compose.yaml`. That's all.

---

## Server requirements

A Linux box with Docker and Compose v2, and a user that can talk to the daemon:

| | |
|---|---|
| Docker | 29.8.2 (tested) |
| Compose | v2.40.3 (tested) |
| RAM | ~1 GB per client (its app and its database) |
| sudo | passwordless, or a wrapper |
| `ss` | ships with iproute2 |
| `sshpass` | optional: without it the SFTP login check is skipped |

Every client gets **its own database in its own container**. There is no shared
database, and that is what makes the isolation real instead of a `GRANT` that
promises it. It costs RAM; while the client count stays low, there is plenty.

The CLI runs on the host, next to Docker: it needs the daemon and its socket, and
the `flock` on the port registry has to sit on the same filesystem as the
registry itself.

It does not deal with TLS. A reverse proxy in front handles that, and it is not
part of this: the stacks serve HTTP on a host port, and whoever publishes them
decides the certificate.

---

## Installation

No Python dependencies. Standard library and nothing else: no `pip install`, no
`requirements.txt`, no `setup.py`.

From the host it needs `docker` with Compose v2, `ss`, passwordless `sudo` for
`destroy`, and `sshpass` — the last one only so the SFTP check can automate the
login. Without `sshpass` everything works, the check is skipped, and `doctor`
tells you so.

```bash
git clone https://github.com/kastormdz/appctl.git
cd appctl
./bin/appctl doctor
```

If `doctor` says OK, you're good.

**The CLI looks for `lib/` and `stacks/` relative to itself**, so it works from
the clone and not from wherever you copied it. Install it where you cloned it:

```bash
sudo ln -s /where/you/cloned/appctl/bin/appctl /usr/local/bin/appctl
```

The symlink resolves correctly because `Path(__file__).resolve()` follows the
link back to the real file, so the folders are found either way.

**Copying just the binary does not work.** If you `cp bin/appctl
/usr/local/bin/` you get an `ImportError` about `ports` with no explanation: the
binary looked for `/usr/local/lib` and it isn't there.

To try it without touching the system:

```bash
export APPCTL_PROJECTS=/tmp/appctl-testing
./bin/appctl acme php psql sftp
```

---

## Layout on the host

Everything hangs off one configurable directory, with a subdirectory per client.
It comes from `APPCTL_PROJECTS`, and if that isn't in the environment it is read
from `/etc/default/appctl`:

```text
$APPCTL_PROJECTS/<project>/            # one per client
├── compose.yaml
├── .env                              # 0600
├── nginx/site.conf                   # NOT loaded today: see Hardening
├── db/init/                          # initdb scripts
├── db/data/                          # database volume
├── sftp/home/upload/                 # the client's code
├── sftp/home/private/                # SFTP only (private: created by the entrypoint)
├── backups/                          # dumps (private: created by the first db dump)
└── state.json
```

The default is `/srv/appctl`, but what matters is where: **on the same
filesystem as the Docker root**. A client project kept away from where its data
lives is a restore that doesn't close.

`db/data` has to be writable by the database engine, so it goes in 755 and not
700. That detail cost an afternoon: with the volume at 700 the container still
reported `healthy`, because the healthcheck is a `pg_isready` that answers before
the engine opens its own data files, and the backups failed without anything
noticing. That is why `create` now runs a real query as the app role instead of a
ping.

The port registry lives separately, in `$APPCTL_HOME` (by default
`/srv/appctl/.appctl`), hidden, so listing projects shows only clients.

| Variable | Default | Purpose |
|---|---|---|
| `APPCTL_PROJECTS` | `/srv/appctl` | project root (also read from `/etc/default/appctl`) |
| `APPCTL_HOME` | `/srv/appctl/.appctl` | port and version registry |
| `APPCTL_HOST` | the machine's hostname | the host shown in the summary |
| `HTTPS_PROXY` | — | needed if the network can't reach the registries |

On `APPCTL_HOST`: if you don't set it, the summary uses the machine's name, not
`localhost`. A hostname resolves the same from outside as long as DNS does;
`localhost` never resolves on the developer's machine. The value gets frozen into
`state.json` when you create the project, so if you created it without
`APPCTL_HOST` and the summary says `localhost`:

```bash
appctl <p> set-host <server-name>
```

`info` and `creds` warn you when the stored host is unusable, with that command
ready.

---

## The stacks

Each stack is a directory under `stacks/`, with its `compose.tmpl.yaml` and its
own image (Dockerfile, entrypoint, nginx) where needed. Adding one means copying
a directory and adjusting the placeholders.

There are **6**, and each brings up **two containers**: `app` and `db`.

| Stack | App | Database | Notes |
|---|---|---|---|
| `php-postgres-sftp` | PHP 8.5 + nginx + sshd | PostgreSQL 18 | the default |
| `php-mysql-sftp` | PHP 8.5 + nginx + sshd | MariaDB 11.8 | |
| `nextjs-postgres-sftp` | Node 22 + Next.js | PostgreSQL 18 | multi-stage build, `.next` |
| `nextjs-python-postgres-sftp` | Node 22 + Next.js + Python API (FastAPI) | PostgreSQL 18 | two runtimes in one container: Next serves `/api` (the BFF layer) and the Python API stays internal |
| `express-postgres-sftp` | Node 22 + Express + React | PostgreSQL 18 | nginx serves the React build and proxies `/api` to Express |
| `tomcat-postgres-sftp` | Tomcat 11 + JDK 25 | PostgreSQL 18 | the external proxy talks straight to Tomcat |

SFTP is not a separate container: it's an `sshd` inside the app container, with
the client's directory mounted on both sides. Fewer containers to operate, less
surface area.

Missing are the `mysql` variants for Next.js and Tomcat, plus a stack with `cron`
for queue workers. The CLI already accepts those names; what doesn't exist yet is
the directory. They aren't built by design: each one gets added when someone
actually asks for it, so the repo doesn't carry stacks nobody has tested.

### Next.js + Python API

`python` isn't a standalone runtime: it's a modifier on `nextjs`. The stack has a
**single image** with Node and Python, a single nginx and a single port:

```text
/        -> Next standalone   (127.0.0.1:3000)
/api/    -> Next               (route handlers: the BFF layer)
API py   -> 127.0.0.1:8000    (internal: the BFF consumes it)
```

The browser **never reaches the API**: `/api` is answered by Next's route
handlers (the BFF layer), the only ones that call the API from inside the
container (`PY_API_URL=http://127.0.0.1:8000`). If you need to publish it
(Swagger, another system consuming it), set `PY_API_PUBLICA=1` in the project's
`.env` and recreate the stack: nginx exposes it at `/api/`.

```bash
appctl acme nextjs python psql sftp
```

Why together and not two stacks: the client has **one** project, **one** port and
**one** SFTP. Two containers would mean two ports to open, two chroots to
maintain, and the code split across two uploads. With `python` next to `nextjs`
the developer uploads everything over the same session:

```text
/upload/              the Next app (package.json, app/, etc)
/upload/backend/      the API (main.py + requirements.txt)
```

| Detail | How it works |
|---|---|
| API startup | `uvicorn main:app` (or `app.main:app` if you use an `app/` package), with the image's venv |
| dependencies | `backend/requirements.txt`, installed when the container starts |
| database | the project's own, via the `db` host — there is no second database |
| prefix | `/api` is **stripped** when proxying: your `/items` is served at `/api/items` |
| `/healthz` | the client must answer it (in Next and in the API): otherwise the container reports `unhealthy` |

Dependencies go into a venv (`/opt/venv`) rather than the system Python because
Alpine marks its own as *externally managed* (PEP 668), where a plain `pip
install` fails. `fastapi` and `uvicorn` ship preinstalled so the verification stub
works **with no network** on a fresh `create`.

The healthcheck hits `/healthz` **and** `/api/healthz`. A check that only looks at
Next leaves the container green with the API down — exactly the kind of green that
is worth nothing.

### Express + React

The stack for "Express backend + React frontend", which is what gets asked for
often. One image with Node 22, one nginx and one port:

```text
/        -> the REACT BUILD (static, with SPA fallback)
/api/    -> Express          (127.0.0.1:3000)
```

```bash
appctl acme express psql sftp
```

The client uploads **two folders** over SFTP to `/upload`:

```text
upload/backend/     package.json + server.js (the Express app)
upload/frontend/    the React app: source, or already built in dist/ (or build/, out/)
```

Startup handles both cases: if the backend already has `node_modules` and the
frontend already has a build with `index.html`, it just starts; otherwise it
installs (`npm ci`) and builds (`npm run build`) by itself. The backend command
is whatever is there: the `package.json` `start` script wins, otherwise
`server.js`, `index.js` or `app.js`.

Three decisions that show up in production:

- **nginx serves the build as static files and proxies `/api`** to the backend
  without eating the prefix: the client's routes (`app.get('/api/x')`) arrive
  intact.
- **SPA fallback for routes only**: `/clients/7` returns `index.html` (React
  Router handles that in the browser), but a `.js` that doesn't exist returns
  **404**, not the HTML — otherwise the browser gets HTML where it expects a
  module and the visible error is `Unexpected token '<'`, which says nothing
  about the cause.
- **The healthcheck watches nginx** (answering any 1xx-4xx), not an app
  `/healthz`: tied to the app's file, any client whose code lacks it stays
  `unhealthy` forever the moment they upload their real app.

The stub of a freshly created project **is real Express** (installed in the
image, not at startup) and answers `/api/healthz` with the Express version, the
node version and the database status: verifying a new stack has to test the
stack, not a text placeholder.


If the network is closed, `create` stores `HTTP_PROXY`/`HTTPS_PROXY` in the
project's `.env` when you have them in your environment (`appctl` does not
invent a proxy): `npm ci` runs at **container startup**, so the proxy has to be
there and not only on the machine where the image is built. Without it, a
project with real code never starts and the only visible symptom is
`npm ERR! network`.

### Tomcat

Tomcat changes the shape of the stack because **it is not PHP**: there's no
`.php` file to serve, there's a `.war` to build and deploy.

| | `php` | `tomcat` |
|---|---|---|
| code | the client uploads it and it's served as-is | a `.war` built with `mvn package` or `gradle build` |
| where it lives | `sftp/home/upload/` | `webapps/<project>.war` on the host |
| path | nginx → `php-fpm:9000` | the external proxy → `tomcat:8080` |

There's no nginx inside: the external proxy talks straight to Tomcat. It's the
simplest thing that works, and since the proxy is in front for TLS anyway, an
nginx inside would be an extra hop for no gain.

Image: `tomcat:11-jdk25-temurin-noble` (Tomcat 11 = Jakarta EE 10, needs Java 17
or newer). One thing worth knowing: Tomcat 10 moved to Jakarta and is **not**
binary compatible with Tomcat 9 (`javax.*` → `jakarta.*`). A `.war` that builds on
9 today won't run on 11 without touching imports. Ask the client which Tomcat
version rather than assuming the latest.

### MySQL

`mysql` is an alternative to `psql`, never additional. With PHP the usual choice
is **MariaDB 11.8**: GPL, no license friction, better embed. MySQL 8.4 (Oracle)
only if the client asks for it explicitly. With Tomcat or Next.js, MySQL 8.4
makes more sense.

---

## Components

| Component | What it adds |
|---|---|
| `php` | PHP-FPM 8.5 + nginx + sshd, code volume |
| `nextjs` | Node 22 + Next.js build, app volume |
| `python` | FastAPI + uvicorn API next to Next, **internal** (the BFF consumes it; `PY_API_PUBLICA=1` publishes it) (modifier: `nextjs` only) |
| `tomcat` | Tomcat 11 + JDK 25, the external proxy talks straight to it |
| `psql` / `mysql` | PostgreSQL 18 / MariaDB 11.8, private network |
| `sftp` | sshd with chroot, dedicated port, volume shared with the app |
| `cron` | the client's crontab, run inside the app container |

`cron` is a flag, not a stack: it changes neither the image nor the database, it
just makes the entrypoint enable the crontab the client uploads to
`/upload/cron/crontab`. It's there because most PHP requests that involve Laravel
or WordPress end up needing a worker, and without this the first "I need to run a
cron" becomes a support ticket. It requires `sftp`, which is how the client
uploads the file.

`python` is the symmetric exception: it isn't a standalone runtime either (there
is no Python-only stack with a database and SFTP), but it **does** change the
image — the `nextjs-python-postgres-sftp` stack ships Node and Python. That's why
it goes into the stack name and not into the list of runtimes: `appctl p python
psql sftp` is rejected with a message telling you what to use, instead of failing
later on a stack that doesn't exist.

```bash
appctl acme php psql sftp
appctl acme php mysql
appctl acme nextjs psql sftp --app-port 3001
appctl acme nextjs python psql sftp
appctl acme php psql sftp cron --app-memory 2G
```

**`create` options:**

| Option | Default | What it does |
|---|---|---|
| `--app-port N` | first free from 8001 | HTTP port for the project |
| `--sftp-port N` | 2221 | SFTP port for the project |
| `--php VER` | default tag | pins the PHP version, 8.1 through 8.9 |
| `--app-memory M` | 512M (1G on Tomcat) | RAM ceiling for the app container |
| `--db-memory M` | 512M | RAM ceiling for the database container |
| `--host NAME` | `$APPCTL_HOST` | host shown in the summary |
| `--dry-run` | — | shows what it would do, touches nothing |

**Invariants checked before creating:**

- `project` matches `[a-z0-9][a-z0-9-]{1,30}[a-z0-9]`
- there's at least one runtime (`php`, `nextjs` or `tomcat`) and at least one
  service (`psql`, `mysql` or `sftp`)
- `psql` and `mysql` are alternatives, never stacked
- the app port and the SFTP port are free, in the registry and on the real network
- the project doesn't already exist
- the resulting stack exists under `stacks/`

---

## The registry: which version gets installed

Tags are resolved at `create` time; they're not hardcoded:

```bash
appctl acme php psql sftp            # PHP 8.5 + PostgreSQL 18
appctl acme php psql sftp --php 8.3  # pinned to 8.3
```

The floating tag lives in `$APPCTL_HOME/versions.json`, behind a write `flock`.
The rule is that **the exact tag is resolved once and written into both the
compose file and `state.json`**. A container that is running never updates
itself: updating is `appctl <p> upgrade`, explicitly, on purpose.

The reason is concrete. `restart: unless-stopped` doesn't re-pull, but a `docker
compose pull` inside a future `up` would. If the tag floated, client A could wake
up on PHP 8.6 on a random Tuesday because someone ran `up`, without anyone
deciding that. Pinned in the YAML, it can't happen.

---

## What gets created, and with which permissions

| Object | Name | Permissions |
|---|---|---|
| Database | `<project>_db` | — |
| App role | `<project>` | `SELECT`, `INSERT`, `UPDATE`, `DELETE` and `CREATE TEMPORARY TABLE` |
| Migration role | `<project>_mig` | everything, including `CREATE SCHEMA` and `DROP` |
| Read-only role | `<project>_ro` | `SELECT` |
| Engine superuser | `<project>_admin` | never shown in the summary |
| SFTP user | `<project>` | chrooted to its home, `upload/` writable |
| `.env` | 0600 | secrets in the clear, never in `compose.yaml` |

In PostgreSQL, on top of `CREATE ROLE` and `CREATE DATABASE OWNER`, the init runs
`REVOKE CONNECT ON DATABASE ... FROM PUBLIC`: without it, any role in the cluster
can connect to a client's database. MariaDB has no roles, so the init creates the
same three **users** with host `'%'` (without a host they'd only get in over a
local socket) and privileges scoped to the project's database, never to `*.*`.

**Temporary** tables are the case that `REVOKE` takes with it: in PostgreSQL the
`TEMP` privilege on the database is granted to `PUBLIC` by default, and revoking
it from `PUBLIC` (which is the right thing to do) also strips it from the
project's roles. The init grants it back explicitly to the app role and the
migration role: a temporary table lives in the session's temporary schema, nobody
else can see it, it dies with the connection and it grants no write on persistent
data. The **read-only role does not get it** — read-only means read-only, and a
gate checks that.

### Why the roles are separate

The app role doesn't need `CREATE SCHEMA` or `DROP`, but `artisan migrate` does.
If they were one role, a SQL injection in the application could drop the whole
schema. They're two roles with two different passwords, and the developer gets the
migration one separately, with instructions to use it only for migrating.

Passwords come from `lib/gen.py` with a deliberately reduced alphabet: letters,
digits, and symbols that break nothing. No quotes, backslashes, `$`, `;` or
spaces, because a password like that breaks the `.env`, a shell command, a
connection URL, a `.pgpass`, or a JDBC string. A client who copy-pastes and fails
at two in the morning is a ticket we'd rather not have.

---

## The summary the developer gets

`create` prints a plain-text summary once, meant to be forwarded without editing.
On the `nextjs-python` stack the summary adds the API block (URL under `/api/`,
where the backend goes, and what it must answer):

```text
API Python  (FastAPI + uvicorn)
  URL        http://127.0.0.1:8000   (INTERNAL: the BFF consumes it)
  Codigo     $APPCTL_PROJECTS/acme/sftp/home/upload/backend   (sube por SFTP)
```

```text
acme  (php-postgres-sftp)
--------------------------------------------------------------
Access to the application
  URL        http://apps.example.com:8001
  Code       $APPCTL_PROJECTS/acme/sftp/home/upload   (uploaded over SFTP)

SFTP access
  Host       apps.example.com
  Port       2221
  User       acme
  Password   ...
  Folder     /upload/            (inside the chroot)
  Command    sftp -P 2221 acme@apps.example.com

Database  (reachable only from the app)
  DB        acme_db

  Application user
    User     acme
    Pass     ...
    Rights   SELECT / INSERT / UPDATE / DELETE
    (no CREATE or DROP: it cannot delete the schema)

  Migration user  (for artisan migrate, etc)
    User     acme_mig
    Pass     ...
    Rights   ALL on the database

  Read-only user  (reports, backups)
    User     acme_ro
    Pass     ...
    Rights   SELECT

--------------------------------------------------------------
Credentials in  $APPCTL_PROJECTS/acme/.env   (0600)
Manage          appctl acme creds
Rotate          appctl acme rotate sftp
```

Afterwards you read the credentials again with `appctl <p> creds`, which prints
them to the screen and leaves them in the scrollback: worth keeping in mind.

The summary uses the hostname, not the IP. The developer copies it and it works;
if the server's address changes tomorrow, the summary isn't stale.

---

## The database

The database **never opens itself**. It lives on an `internal: true` network with
no published port, and `create` verifies that for real: it tests from another
container in the project and from the host, rather than reading the config.

### The two networks

Each project has two, and the fact that there are two is the point:

```yaml
networks:
  # with internet egress: the app calls a payment gateway, an SMTP server,
  # a WhatsApp API. Without this the client can't get paid.
  egress:
    driver: bridge
  # no gateway. The guarantee that the database isn't visible from outside,
  # built with machinery instead of "I didn't add a ports:".
  data:
    driver: bridge
    internal: true
```

`app` is on both: it reaches the internet for `composer` and `packagist`, and it
reaches the database only through `data`. `db` is on `data` alone.

Putting `internal: true` on the whole stack's network would break the apps, which
need to get out. And if both networks were one, the database would be reachable
from anything that can route to the host.

### Backups, export and import

```bash
appctl <p> db dump                 # to <p>/backups/, dated, gzip and sha256
appctl <p> db dump -o /tmp/x.dump.gz   # anywhere you like
appctl <p> db list                 # which backups exist
appctl <p> db restore <file> --yes
appctl <p> db rm <file> --yes      # delete one (no undo)
```

The dump comes from inside the container, is stored gzip-compressed, and uses a
`.dump.gz` suffix: you don't need `psql` on the host, and the database doesn't
need to be reachable. It works on both engines with the same command. Every dump
leaves a dated gzip file plus a `.json` next to it with the engine, compressed size
and sha256.

`restore` **overwrites the database**, so it warns you first and saves the previous
state with the exact command to get back to it.

**`restore` without `--clean`**, on purpose: a `--clean --if-exists` emits
`DROP ROLE` for the source role, and restoring that into a clone dies with
`FATAL: role "x" does not exist`. Only use `--clean` when the destination already
has objects with the same names.

Backups live in `<project>/backups/`, which isn't committed: `.env` and `*.sql`
are in `.gitignore`. A dump contains client data.

### Opening it up

```bash
appctl <p> db expose 5432                     # publishes on 0.0.0.0 (any network that reaches it)
appctl <p> db expose 5432 --bind 127.0.0.1    # this host only (for an ssh tunnel)
appctl <p> db expose 5432 --bind 10.20.0.5    # from a specific host IP
appctl <p> db unexpose                        # close it
```

The default is `0.0.0.0`: if you expose the database it is so a client can get in
from another machine, and publishing only on `127.0.0.1` forces an `ssh -L`
tunnel. The command says so when publishing, with the usable address
(`host:port`) and how to narrow it. The database stays protected: it still asks
for a password, stays on its internal network, and `db grant --cidr` limits which
network each user connects from. `--bind` takes an IP, not a network, because
docker rejects a CIDR in `ports:`; `--bind 127.0.0.1` is for the "tunnel only" case.

The port and the bind are recorded in `state.json`: `appctl <p> info` shows them
and an `upgrade` no longer drops them. They used to live only in the rendered
`compose.yaml`, so the next re-render wiped them silently and the database went
back to closed while the client believed it was still open.

`info` and `creds` show the address a client connects with, not the raw bind.
With `--bind 0.0.0.0` they show the **host** (`dicappsrv…:5435`) and state that
anyone who reaches that address gets in; with `--bind 127.0.0.1` they show
`127.0.0.1:5435` and warn that **nothing from outside gets in** — the host name
there would be an address that answers to nobody. `db expose` says so at publish
time, and `creds` (the summary the developer receives) now carries the database's
exposure state: it used to always claim "the DB is not open to the internet",
which was false once a port was published.

### Database users

```bash
appctl <p> db users                              # what exists and with what rights
appctl <p> db grant consulting --rol readonly     # readonly, write or migrate
appctl <p> db grant dev --rol write --cidr 10.20.0.0/16
appctl <p> db revoke consulting
```

`grant` creates the user with its own password and the role you asked for.

On MariaDB the migration user needs `WITH GRANT OPTION` on the database **and**
global `CREATE USER`. MariaDB rejects `GRANT OPTION ON db.*` with error 1064
because it isn't a privilege, it's an option of `GRANT`; and with only `WITH
GRANT OPTION` you get `1227: you need (at least one of) the CREATE USER
privilege`. The global scope is harmless here because every database is on its own
network and shares nothing with other clients: the network provides the
isolation. On a MySQL with several databases on one server, it isn't.

---

## Commands

### Creating, listing and diagnosing

| Command | What it does |
|---|---|
| `appctl <project> <components...>` | creates a project |
| `appctl create <project> <components...>` | same, explicit |
| `appctl build [--runtime X] [--stack S] [--force]` | builds the base images; run once |
| `appctl list` | table of projects: name, stack, ports, database |
| `appctl ps` | the appctl stacks with their ports and state |
| `appctl doctor` | host state: Docker, registry, proxy, `sudo -n`, `sshpass` |
| `appctl <project> info` | project detail, its images, its services and its RAM/CPU limits |
| `appctl <project> set-host <name>` | fixes the host in the summary |

### Operating a project

| Command | What it does |
|---|---|
| `appctl <project> creds` | prints the `.env` again |
| `appctl <project> logs [-f] [--service app\|db]` | logs, optionally live |
| `appctl <project> shell [--service app\|db]` | shell inside a container |
| `appctl <project> psql [-U user] [-d database]` | `psql` connected, no password prompt |
| `appctl <project> restart` | restarts the services |
| `appctl <project> rotate <sftp\|db\|mig\|ro>` | changes one credential |
| `appctl <project> limits [--service app\|db] [--memory M] [--db-memory M] [--cpus N] [--save]` | adjusts RAM and CPU live, per service or both |
| `appctl <project> upgrade [-y] [--php VER] [--runtime R]` | re-applies the compose from the template (accepts real code already uploaded: asks for HTTP 200, not the stub) |
| `appctl clone <source> <target> [--port N] [--php VER]` | copies a project with its data |
| `appctl <project> destroy [--keep-data] [--yes]` | destroys the project |

### Database

The project comes before the subcommand:

```bash
appctl <project> db dump [-o path]           # export the database as gzip
appctl <project> db restore <file> --yes     # import; OVERWRITES the database
appctl <project> db list                     # backups that exist
appctl <project> db rm <file> -y             # delete a backup
appctl <project> db expose <port> [--bind IP]  # publish (default 0.0.0.0)
appctl <project> db unexpose                 # close it
appctl <project> db grant <user>             # add an external user
appctl <project> db revoke <user> --yes      # take the access away
appctl <project> db users                    # database users and their rights
```

---

## Verified versions

Pulled one by one against Docker Hub, not from memory (2026-10-01):

| Image | Tag | Size |
|---|---|---|
| PHP | `php:8.5-fpm-alpine` | **150 MB** ← what the stack uses |
| PHP | `php:8.5-fpm-bookworm` | 732 MB |
| PostgreSQL | `postgres:18-alpine` | 433 MB |
| MariaDB | `mariadb:11.8` | 458 MB |
| Node | `node:22-alpine` | 238 MB |
| nginx | `nginx:stable-alpine` | 93.6 MB |
| Tomcat | `tomcat:11-jdk25-temurin-noble` | 602 MB |
| Tomcat | `tomcat:10.1-jdk21-temurin-noble` | 729 MB |
| Tomcat | `tomcat:9-jdk21-temurin-noble` | 730 MB |

`php:8.5-fpm-alpine` is 150 MB against 732 MB for bookworm, five times less, and
that decides how many clients fit on the disk. The PHP stack uses alpine. The
extensions clients ask for most (`intl`, `gd`, `zip`, `bcmath`, `soap`) build
there without trouble and ship in the image: there is no need to drop to
bookworm for them. See *What the PHP image ships*.

On Tomcat, tags with `jre` in the name don't exist: only the `jdk` ones do.

---

## PHP and nginx hardening
### What the PHP image ships

Measured inside the container with `php -m` and `extension_loaded()`, not copied
from a Debian package table (`php8.5-*` names are Debian's; here they are
extensions):

| Extension | Why a client asks for it |
|---|---|
| `pdo_pgsql` + `pgsql` | PostgreSQL access (`pdo_mysql` + `mysqli` on the MySQL stack) |
| `mbstring` | UTF-8 strings |
| `intl` | dates, numbers and currencies via ICU; Laravel and Symfony use it |
| `gd` | images: TCPDF, PhpSpreadsheet, thumbnails |
| `zip` | `ZipArchive`: Excel exports (Laravel Excel, zipstream) |
| `bcmath` | exact decimals: JWK/JWT, TCPDF PDF417 barcodes |
| `soap` | legacy SOAP clients |
| `xml`, `dom`, `SimpleXML`, `xmlreader`, `xmlwriter`, `libxml` | XML parsing and generation |
| `curl` | outbound HTTP (`wp_remote_get`, Guzzle) |
| `Zend OPcache` | runtime opcode cache |

Two details that pay for themselves:

- `Zend OPcache` ships **compiled in** the official image, with no `.so` of its
  own: check it by that exact name (`extension_loaded('Zend OPcache')`); looking
  for plain `opcache` returns false. It is not in `php.ini` because it is not
  needed.
- `intl` needs ICU data, and alpine splits it: `icu-data-en` carries only the
  English locales. With that, `IntlDateFormatter('es_AR')` **formats in English**
  with no warning. The image installs `icu-data-full` (906 locales, `es_AR`
  included).

If a client needs an extension that is not on the list, add it to the stack's
Dockerfile (`stacks/php-*-sftp/image/Dockerfile`), rebuild the image and move the
project with `upgrade`. Careful with build dependencies: they go in a `apk --virtual`
group removed at the end, and the *runtime* libraries must stay outside that
group because `apk del` takes the group's packages with it.


The PHP stacks ship with this set from the factory. There's nothing to touch per
project: it's baked into the image.

### Sessions and cookies

| Directive | Value | What it stops |
|---|---|---|
| `session.use_strict_mode` | `1` | a session id invented by the client (session fixation) |
| `session.use_only_cookies` | `1` | the session travelling in the URL |
| `session.cookie_httponly` | `1` | JavaScript reading the cookie (theft via XSS) |
| `session.cookie_secure` | `1` | the cookie travelling over plain HTTP |
| `session.cookie_samesite` | `Lax` | the cookie being sent on cross-site requests |

`cookie_secure=1` works fine with TLS terminated at the proxy in front: the
party deciding whether to send the cookie is the browser, and it sees HTTPS even
though the backend speaks HTTP.

**If the app is used over plain HTTP** (with nothing terminating TLS), that
cookie never travels and the login never sticks. It looks like "the app doesn't
keep the session", and it isn't the app. Fix it per project:

```bash
echo 'APPCTL_SESSION_COOKIE_SECURE=0' >> $APPCTL_PROJECTS/acme/.env
appctl acme upgrade --yes
```

### Remote wrappers

| Directive | Value | What it stops |
|---|---|---|
| `allow_url_fopen` | `Off` | `file_get_contents('http://...')`: the classic RFI and SSRF route |
| `allow_url_include` | `Off` | an `include` pulling code from a URL |
| `cgi.fix_pathinfo` | `0` | `/upload/photo.jpg/x.php` executing the `.jpg` |

Composer, Guzzle and `wp_remote_get` use the `curl` extension (it's in the
image), not `fopen`, so nothing breaks. If you do need `fopen` over URLs:

```bash
echo 'APPCTL_ALLOW_URL_FOPEN=On' >> $APPCTL_PROJECTS/acme/.env
appctl acme upgrade --yes
```

### Directory listing, and the `.htaccess` nobody reads

**nginx ignores `.htaccess` files entirely.** Apache directives — `Options
-Indexes`, a `Deny from all`, a `RewriteRule` — are not read on this stack. They
are files that do nothing. If the app relies on an `.htaccess` for something,
that something is not happening, and it's worth knowing before trusting it.

Directory listing is closed anyway, because nginx **does not list by default**
(Apache does, which is why it needs `Options -Indexes`). A request for a
directory with no `index` answers `403`. The vhost writes the `autoindex off`
out explicitly and adds the headers:

| Directive | Value |
|---|---|
| `autoindex` | `off` |
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `SAMEORIGIN` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |

HSTS doesn't belong here: TLS is terminated at the proxy in front, and a
`Strict-Transport-Security` emitted by a backend that speaks HTTP never reaches
the browser. The proxy has to emit it.

`.htaccess`, `.env` and anything starting with a dot isn't served either: the
vhost denies it.

### Where this config lives

The nginx vhost is **baked into the image** (`image/nginx.conf`). Changing it
means `appctl build` and then `appctl <project> upgrade`. The
`nginx/site.conf` in each project directory **is not loaded today**: nginx
doesn't include `conf.d/`, so editing it has no effect. Noted as pending.

### Private directory (SFTP only)

Each project has a `sftp/home/private/` next to `upload/`: the client sees it
over SFTP as `/private` and keeps there whatever must not go out over the web. On
the PHP stacks nginx roots at `/upload`, so it never serves it; on top of that an
explicit `location` denies it and `disable_symlinks on` stops the
`upload/link -> ../private` symlink trick. On `nextjs-python` nginx is a pure
proxy (no root), and the `deny` stays anyway as defence in depth: the day someone
adds a server block with a root, the barrier is already there. It belongs to the
SFTP user, mode 700: neither nginx nor php-fpm gets in. From PHP it is reachable
over the filesystem at `/srv/sftp/private`, if permissions allow.

Over SFTP the client only sees `/upload` and `/private`, nothing else. SFTP runs with `ForceCommand
internal-sftp` inside sshd itself, so the chroot carries no binaries: no
`bin/`, `etc/`, `lib/` or `usr/` in view. `/tmp` is no longer created (it
was scratch); on old projects it is removed only if empty — if the client
left files there, it stays. On projects created before this
change, the entrypoint deletes those scaffolding directories at boot (explicit
list, never the client's).

The entrypoint creates it with `mkdir -p` and never `rm -rf`: deleting there
would hit the host's real disk through the bind mount. That was the cause of
a bug where every restart wiped the client's `upload`.

---

## What `create` verifies

After bringing the stack up, `create` runs ten checks and reports **all of them**,
not just the first failure: when something's broken at three in the morning, you
want the whole list in one go. On the `nextjs-python` stack it's **eleven**: the
API is verified separately (see point 9).

1. The `db` and `app` containers are `healthy`.
2. The project's three roles exist.
3. The database **rejects** connections without a password, meaning `pg_hba`
   didn't end up in `trust`.
4. The database is usable: a real query as the app role, not a ping.
5. The database answers from the internal network.
6. The database is **not reachable from the host**.
7. The database is **not reachable from another project's network**, hitting its
   real IP. The name `db` resolves to a different database and would give a false
   pass.
8. The app answers what it should, depending on the stack's runtime.
9. The API answers (`nextjs-python` only). It's a separate check because nginx
   serves two runtimes on the same port: a 200 on `/` comes from Next, so it
   proves nothing about Python, and with uvicorn down `/` still returns 200. By
   default the API is **internal** and the check asks for it inside the
   container (`127.0.0.1:8000`); if the project published it
   (`PY_API_PUBLICA=1`), it asks through the port, at `/api/`.
10. The SFTP login works: it gets in, lists, and **writes** a file. A read-only SFTP
    is no use for uploading code, and a successful `ls` doesn't detect it.
11. The compose state has no drift.

Points 6 and 7 are the ones that matter. A port not appearing in `compose.yaml`
isn't a guarantee: it's an absence of configuration, and anyone can edit the
configuration. What's checked is the behaviour. If some database image ever ships
its own `ports:`, creation stops there instead of three months later.

---

## Idempotency and concurrency

- `flock` on `registry.json` for the whole of `create` and `destroy`. Without it,
  two creations at once read the same free port, both reserve it, and one ends up
  with a failing container and a registry that lies.
- Both ports (app and SFTP) get reserved in the registry **before** anything is
  started.
- On top of the registry, `ss -ltnp` is checked: the registry can lie if someone
  started a container by hand, and the network gets the benefit of the doubt over
  the file.
- System service ports (22, 80, 443, 3306, 5432, 8080) are never assigned, even
  when free.
- `create` on an existing project fails. `destroy` requires `--yes` or an
  interactive confirmation.

---

## Disk: the real limit, before RAM

A `php + postgres` stack comes to around 800 MB of images. With data, logs and
backups, each client realistically takes 1.5-2 GB. That gives you roughly 15
clients with room to spare on a 32 GB volume, not the 30 you get from the
optimistic RAM-only arithmetic.

Once you reach 10 clients, measure actual usage and only then decide: grow the
volume, or move the data elsewhere. Before that, there's nothing to decide.

---

## Repo layout

```text
appctl/
├── bin/appctl                 # the CLI, stdlib and nothing else
├── lib/
│   ├── gen.py                 # renders the compose and the .env
│   ├── ports.py               # port registry + flock
│   ├── dbtool.py              # dump / restore / expose / grant
│   ├── smoke.py               # the checks (11 on nextjs-python)
│   └── summary.py             # the summary for the developer
├── stacks/                    # 6 stacks, one per directory
└── tests/
    ├── test_init_sql.py       # runs the inits and validates the SQL they produce
    ├── test_root_pw.py        # parses the password line out of the mariadb log
    ├── test_security.py       # the compose doesn't publish the database
    ├── test_php_hardening.py  # cookies, headers, and the .htaccess nobody reads
    ├── test_private_sftp.py   # the private dir: 700, deny, and no rm -rf
    ├── test_sftp_chroot.py    # the chroot carries upload and private only
    ├── test_nextjs_python.py  # the two-runtime stack: nginx, uvicorn, chroot
    ├── test_app_check_status.py     # the published port: state, info and the bind
    ├── test_db_publish.py     # the published port: state, info and the bind
    ├── test_readme.py         # every README claim against the code
    └── check_names.py         # AST: undefined calls, duplicated defs
```

Two of these tests exist because the software was lying to you:

`test_init_sql.py` runs the inits for real, with `psql` and `mariadb` replaced by
a binary that does `cat`, and looks at the SQL that comes out. An init runs once,
when the volume is created: if it fails there there's no retry, and the project is
left with a database that never gets its three users.

`test_readme.py` pulls every verifiable claim out of this README (the stacks, the
flags, the subcommands, the versions, the defaults, the networks) and checks it
against the files. It exists because this README was wrong: it said there were 8
stacks when there were 4, listed two `lib/` modules that were never written, and
documented a flag the parser never defined. None of that shows up by reading the
document; it shows up by running the test.

---

## Status

Working end to end, verified with both PostgreSQL and MariaDB:

| | |
|---|---|
| `create` | 10/10 checks: containers healthy, database answering, roles created, password required, database unreachable from the host and from another project |
| `clone` | new data, indexes and credentials; the clone is independent of the source |
| `upgrade` | PHP 8.5 → 8.4 → 8.5, data intact |
| `db dump` / `restore` | PostgreSQL and MariaDB |
| `db expose` | publishes the database on a host port, by default on `0.0.0.0` (any network that reaches it; `--bind 127.0.0.1` for this-host-only); the port is recorded and an `upgrade` no longer drops it |
| `db grant` | adds external users, each with its own password |

All four stacks were verified end to end, including `tomcat-postgres-sftp`.