# appctl

One command and the client's server is up.

```bash
appctl acme php psql sftp
```

Brings up nginx + PHP 8.5 + PostgreSQL 18 + SFTP, publishes only the ports that
are actually needed (the app's and SFTP's), leaves the database unreachable
from outside, and hands you a summary with everything the developer needs.

[Español](README.md)

---

## What this is and what it isn't

It's a provisioner. You tell it what the client wants and you get a running
stack, which deletes in one command.

It isn't a PaaS. No web panel, no git push deploys, no billing, no multi-server.
If you need that, Coolify and Dokploy already do it, and you'll be the one
running and updating it. The problem here is a different one: a client asks for
PHP with PostgreSQL and SFTP, and that exists today, not three days from now.

Each client is a directory with a `compose.yaml`. That's it.

---

## Server requirements

Linux with Docker and Compose v2, and a user that can talk to the daemon:

| | |
|---|---|
| Docker | 29.8.2 (tested) |
| Compose | v2.40.3 (tested) |
| RAM | ~1 GB per client (its app plus its database) |
| sudo | passwordless, or a wrapper |
| `sshpass` | optional: without it the SFTP check gets skipped |

Every client gets **its own database in its own container**. There's no shared
database. That's what makes the isolation real instead of a `GRANT` that merely
promises it. It costs RAM; while the client count is small, there's plenty.

The CLI runs on the host, next to Docker. It needs the daemon and the socket,
and the `flock` on the registry has to live in the same filesystem as the
registry itself.

TLS isn't its job. A reverse proxy in front handles that, and that isn't part of
this. The stacks serve HTTP on a host port and whoever publishes them decides the
certificate.

---

## Installation

No Python dependencies: it's stdlib and nothing else. No `pip install`, no
`requirements.txt`, no setup.py.

From the host it needs `docker` with Compose v2, `ss` (ships with iproute2),
`sudo` without a password for `destroy`, and `sshpass`. That last one is only
for automating the SFTP login in the check. Without `sshpass` everything still
works, the SFTP check just gets skipped, and `doctor` says so.

```bash
git clone https://github.com/kastormdz/appctl.git
cd appctl
./bin/appctl doctor
```

`doctor` tells you whether the host has what it takes. If it says OK, you're
good.

**The CLI looks for `lib/` and `stacks/` relative to itself**, so it works from
the clone and not from wherever you copied it to. In practice that means
installing it where you cloned it:

```bash
sudo ln -s /where/you/cloned/appctl/bin/appctl /usr/local/bin/appctl
```

The symlink resolves fine: `Path(__file__).resolve()` follows the link through
to the real file, so `lib/` and `stacks/` are found either way.

**Copying just the binary doesn't work.** `cp bin/appctl /usr/local/bin/` gets
you an `ImportError` about `ports`, with no explanation: the binary looked for
`/usr/local/lib` and it isn't there.

If you'd rather have the symlink somewhere else, or a wrapper, go for it. What
doesn't work is the lone binary.

To try it without touching the system, point `APPCTL_PROJECTS` at a temporary
directory:

```bash
export APPCTL_PROJECTS=/tmp/appctl-testing
./bin/appctl acme php psql sftp
```

## Layout on the host

Everything hangs off one configurable directory, with a subdirectory per
client. It's set with `APPCTL_PROJECTS`, and if that isn't in the environment
it's read from `/etc/default/appctl` (the standard place for defaults on Linux,
so nobody has to export anything in every shell):

```
$APPCTL_PROJECTS/<project>/          # one per client
├── compose.yaml
├── .env                                 # 0600
├── nginx/site.conf
├── db/init/                             # initdb scripts
├── db/data/                             # database volume
└── state.json
```

The default is `/srv/appctl`, but where it goes matters more than the value:
**on the same filesystem as the Docker root**. A client project kept away from
where its data lives is a restore that never closes.

The port registry sits separately in `$APPCTL_HOME` (default
`/srv/appctl/.appctl`), hidden, so listing projects shows only clients.

| Variable | Default | What it's for |
|---|---|---|
| `APPCTL_PROJECTS` | `/srv/appctl` | projects root (also read from `/etc/default/appctl`) |
| `APPCTL_HOME` | `/srv/appctl/.appctl` | port registry and versions |
| `APPCTL_HOST` | `localhost` | the host printed in the summary |
| `HTTPS_PROXY` | — | needed when the network can't reach the repos |

---

## Syntax

```bash
appctl <project> [components...] [options]
```

## The stacks

Each stack is a directory under `stacks/`, with its `compose.tmpl.yaml` and its
own image (Dockerfile, entrypoint, nginx) where that's needed. Adding one means
copying a directory and adjusting the placeholders.

There are **4**:

| Stack | App | DB | Services | Notes |
|---|---|---|---|---|
| `php-postgres-sftp` | PHP 8.5 + nginx | PG 18 | 3 | the default |
| `php-mysql-sftp` | PHP 8.5 + nginx | MariaDB 11.8 | 3 | |
| `nextjs-postgres-sftp` | Node 22 + Next | PG 18 | 3 | multi-stage build, `.next` |
| `tomcat-postgres-sftp` | Tomcat 11 + nginx | PG 18 | 4 | nginx on its own |

The missing ones (the `mysql` variants of nextjs and tomcat, and the stack with
`cron` for queue workers) are new directories. The CLI already asks for them by
name; what doesn't exist yet is the directory. That's deliberate: each one gets
added when someone actually asks for it, so the repo doesn't carry stacks nobody
has tested.

## The `tomcat` stack

Tomcat changes the shape of the stack, because **it isn't PHP**: there's no
`.php` to serve, there's a `.war` to build and deploy. Three concrete differences
against `php`:

| | `php` | `tomcat` |
|---|---|---|
| code | `/var/www/html`, mounted | `/usr/local/tomcat/webapps/<project>.war` |
| build | client uploads the finished code | `mvn package` / `gradle build` → the `.war` |
| upstream | nginx → `php-fpm:9000` | nginx → `tomcat:8080` |

The compose carries **4 services**: `app` (tomcat), `nginx`, `db` and `sftp`.
Here nginx **does** become a separate service, because it has to terminate TLS
and talk to a `catalina.sh` running on its own. In PHP, nginx and FPM live
together. In Tomcat they can't.

Image: `tomcat:11-jdk25-temurin-noble` (Tomcat 11 is Jakarta EE 10, needs Java
17+). Watch out: Tomcat 10 went Jakarta and is **not** binary compatible with
Tomcat 9 (`javax.*` → `jakarta.*`). A `.war` that compiles on 9 today won't run
on 11 without touching imports. Ask the client which Tomcat they're on instead
of assuming the newest.

## The `mysql` stack

`mysql` is an alternative to `psql`, never an addition. With `php` the usual
choice is **MariaDB 11.8** (GPL, no license friction, better embedded); MySQL 8.4
only when the client asks for it explicitly. With `tomcat` or Next.js, MySQL 8.4
starts to make more sense.

## The registry: which version gets installed

The tags resolve at `create` time, and they're not hardcoded:

```bash
appctl acme php psql sftp          # PHP 8.5.11 + PostgreSQL 18.6 + atmoz/sftp:alpine
appctl acme php psql sftp --php 8.3   # pinned to 8.3
```

The floating tag lives in `$APPCTL_HOME/versions.json`, behind a write lock.
The rule: **the exact tag resolves once and gets written into the compose and
`state.json`**. A container that's running never updates itself, not ever.
Upgrading is `appctl acme upgrade`, on purpose explicit.

## Components

| Component | What it adds |
|---|---|
| `php` | PHP-FPM 8.5 + nginx + supervisor, code volume |
| `nextjs` | Node 22 + Next.js build, app volume |
| `tomcat` | Tomcat 11 + JDK 25, separate nginx reverse proxy, `.war` in `webapps/` |
| `psql` / `mysql` | PostgreSQL 18 / MariaDB 11 (or MySQL 8.4), private network |
| `sftp` | atmoz/sftp, dedicated port, volume shared with the app |
| `cron` | supervisor with a crontab mounted in |

```
appctl acme php psql sftp
appctl acme php mysql
appctl nextjs-project nextjs psql sftp --app-port 3001
```

**Options:** `--app-port N` (default: first free from 8001) · `--sftp-port N`
(default: 2221) · `--host NAME` (default: `$APPCTL_HOST`) ·
`--dry-run` · `--yes`

**Invariants checked before creating anything:**

- `project` matches `[a-z0-9][a-z0-9-]{1,30}[a-z0-9]`
- there's **at least one runtime** (php or nextjs) and **at least one service** (psql, mysql or sftp)
- the app port and the sftp port are free (both the registry **and** real `ss -ltnp`)
- the project doesn't already exist
- the resulting stack exists under `stacks/`

---

## Verified versions (pulled from Docker Hub, 2026-10-01)

Not from memory and not from the website: pulled one by one.

| Image | Verified tag | Size |
|---|---|---|
| PHP | `php:8.5-fpm-bookworm` | 732 MB |
| PHP | `php:8.5-fpm-alpine` | **150 MB** ← what the stack uses |
| PostgreSQL | `postgres:18-alpine` | 433 MB |
| MariaDB | `mariadb:11.8` | 458 MB |
| Node | `node:22-alpine` | 238 MB |
| nginx | `nginx:stable-alpine` | 93.6 MB |
| SFTP | `atmoz/sftp:alpine` | 32.1 MB |
| Tomcat | `tomcat:11-jdk25-temurin-noble` | 602 MB |
| Tomcat | `tomcat:10.1-jdk21-temurin-noble` | 729 MB |
| Tomcat | `tomcat:9-jdk21-temurin-noble` | 730 MB |

`php:8.5-fpm-alpine` is 150 MB against 732 MB for bookworm, five times less.
On its own that's what decides how many clients fit on the disk. The PHP stack
uses alpine; if a client needs an extension that won't compile there (GD with
system libraries, say), it falls back to bookworm and eats the cost.

Tomcat: tags with `jre` in the name **don't exist**
(`11-jdk25-temurin-jre17-noble` won't pull), only the `jdk` ones. And 9, 10 and
11 are all available, which is exactly what lets you ask the client their
version instead of guessing.

## The default stack (`php psql sftp`)

A `compose.yaml` with **three** services (`app`, `db`, `sftp`) and **two**
networks. The networks are the interesting part:

```yaml
networks:
  # with outbound access: the app calls a payment gateway, an SMTP, a
  # whatsapp API. Without this the client can't charge anyone.
  egress:
    driver: bridge
  # NO GATEWAY. The guarantee that the database isn't visible from outside,
  # built with machinery instead of "I didn't add a ports:".
  # Nobody reaches it, not even a container from another project.
  data:
    driver: bridge
    internal: true
```

`app` sits on both networks: it reaches the internet for `composer` and
`packagist`, and it reaches the database only over `data`. `db` sits only on
`data`.

Merging them into one network would make the database reachable from anything
that can route to the host. That's the whole reason there are two.

---

## What's generated, and with what permissions

| Object | Name | Permissions |
|---|---|---|
| Database | `<project>_db` | — |
| App role | `<project>` | `ALL` on `_db`, `CONNECT` on `_db` |
| Migration role | `<project>_mig` | `ALL` on `_db` (for Laravel's `artisan migrate`) |
| Read-only role | `<project>_ro` | `SELECT` on `_db` |
| SFTP user | `<project>` | chrooted to its home, `upload/` writable |
| `.env` | 0600, root:root | secrets in plain text, never in the YAML |

In MySQL: `GRANT ALL ON \`acme_db\`.*` for `<project>` and `<project>_mig`, plus
a `<project>_ro` with `SELECT`. In PostgreSQL: `CREATE ROLE` + `CREATE DATABASE
OWNER` + `REVOKE CONNECT ON DATABASE ... FROM PUBLIC` (without that last one,
any role in the cluster can connect to a client's database).

### Separate roles, and why

The app role does **not** need `CREATE SCHEMA` or `DROP`. `artisan migrate` does.
Mixing them means a SQL injection in the app can drop the schema. So: two roles,
two passwords, and the developer gets the migration one separately, with
instructions to use it only for migrating.

---

## The summary (output of `create`)

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

It prints **once**. After that you read it with `appctl <project> creds`.

---

## Disk: the real limit

A `php + postgres` stack runs about 800 MB of image. With data, logs and
backups, each client lands at 1.5-2 GB in practice. Tell me how many clients you
want to carry and I'll tell you whether the disk holds; if it doesn't, we either
grow it or move the data volumes off.

---

## Database commands

The database is **never exposed on its own**. It lives on an `internal: true`
network with no published port, and the `create` check proves it: it tests from
another container, not by reading the config.

### Exposing it

```bash
appctl <p> db expose 5432                     # only from 127.0.0.1
appctl <p> db expose 5432 --cidr 10.20.0.0/16  # open to a network
```

The default is `127.0.0.1`, not a network. With just any `/8`, every LAN that
can reach the port gets in, and whoever ran the command has no way to know the
database ended up open to half the environment. For a network, `--cidr`
explicit.

`appctl <p> db unexpose` closes it. Publishing the port doesn't take the database
off its internal network: that's a host publication, not a permission.

### Backups

```bash
appctl <p> db dump                 # to a file, both engines
appctl <p> db restore <file>
```

On Postgres the dump carries `--no-owner --no-acl` and **no `--clean`**: a
`--clean --if-exists` emits `DROP ROLE` for the source role, and restoring that
into a clone dies with `FATAL: role "x" does not exist`.

### Granting a user

```bash
appctl <p> db grant name --rol readonly
```

In MariaDB the migration user needs `WITH GRANT OPTION` on the database **and**
global `CREATE USER`. MariaDB rejects `GRANT OPTION ON db.*` with 1064 because
it isn't a privilege: it's an option of `GRANT`. The global scope is harmless
here because every database is on its own network and shares nothing with other
clients, and the network is what provides the isolation. On a MySQL with several
databases on one server, it isn't.

---

## Commands

```bash
appctl <project> <components...>   # create
appctl list                        # table: project | app | sftp | db | status
appctl <project> info               # urls + per-service status
appctl <project> creds              # re-reveal the .env (ends up in scrollback)
appctl <project> logs [-f] [--app|--db|--sftp]
appctl <project> shell              # exec bash in the app container
appctl <project> psql               # psql, connected, no password
appctl <project> restart
appctl <project> rotate <sftp|db|mig>
appctl <project> destroy [--keep-data]
appctl doctor                        # host state
```

---

## Idempotency and concurrency

- `flock` on `registry.json` for the whole of `create`/`destroy`. Without it, two
  parallel creations grab the same port without noticing.
- Both ports (app and sftp) get reserved in the registry before anything starts.
- It checks real `ss -ltnp` as well as the registry: the registry can lie if
  someone started a container by hand.
- `create` on an existing project fails. `destroy` needs `--yes` or an
  interactive confirmation.

### The check that matters most

After `up -d`, the smoke test **checks that the database doesn't answer from the
host**:

```bash
docker run --rm --network host postgres:18-alpine \
  pg_isready -h 127.0.0.1 -p 5432   # this has to fail
```

The port not being in the YAML isn't enough: you have to confirm that a client
from outside dies. If a database image ever ships with a `ports:` in it, that
stops at creation instead of three months later.

---

## Repo layout

```
appctl/
├── bin/appctl                 # the CLI, stdlib and nothing else
├── lib/
│   ├── gen.py                 # renders the compose and the .env
│   ├── ports.py               # port registry + flock
│   ├── dbtool.py              # dump / restore / expose / grant
│   ├── smoke.py               # the 10 checks
│   └── summary.py             # the summary for the developer
├── stacks/                    # 4 stacks, one per directory
├── tests/
│   ├── test_init_sql.py       # runs the inits and validates the SQL they produce
│   ├── test_root_pw.py        # parses mariadb's log line
│   ├── test_security.py       # the compose doesn't publish the database
│   ├── test_readme.py         # every README claim, checked against the code
│   └── check_names.py         # AST: undefined calls, duplicate defs
└── docs/                      # (private, not in this repo)
```

`tests/test_init_sql.py` paid for itself several times over. An init runs once,
when the volume is created; if it fails there, there's no retry and the project
ends up with a database missing its three users, permanently. The test runs the
init for real with `psql`/`mariadb` swapped for a binary that does `cat`, and
looks at the SQL that comes out. No database required.

`create` runs 10 checks. The last one is "the database can be used": it runs a
real query with the app role's password. There were 9 before, and the healthcheck
was a `pg_isready`, which answers even when the engine can't open its own data.
With the database volume at 700, postgres restarted in a loop
(`could not open file "global/pg_filenode.map"`) and everything looked fine: the
backups failed and nothing gave it away.

`tests/test_readme.py` is what keeps this README from lying. It pulls out every
checkable claim (the stacks, the flags, the subcommands, the versions, the
default paths, the networks) and compares it against the files. It exists
because this README was wrong: it said there were 8 stacks when there were 4,
and listed two lib/ modules that were never written, and documented an environment-file flag
the parser never defined. None of that shows up by reading the
document. It shows up by running the test.



---

## Status

Working and verified end to end on PostgreSQL and MariaDB:

| | |
|---|---|
| `create` | 10/10 checks: containers healthy, DB answers, roles created, password required, DB unreachable from the host and from another container |
| `clone` | data, indexes and new credentials; the clone is independent of the source |
| `upgrade` | PHP 8.5 → 8.4 → 8.5, data intact |
| `db dump/restore` | Postgres and MariaDB |
| `db expose` | publishes the database on a host port; only from `127.0.0.1` by default, `--cidr` opens it to a network |
| `db grant` | new users, with their own password |

Verified stacks: `php-postgres-sftp`, `php-mysql-sftp`,
`nextjs-postgres-sftp`, `tomcat-postgres-sftp`.

---

## The server name in the summary

The summary prints the `$APPCTL_HOST` name, not the IP. The developer copies it
and it works. If the server's IP changes tomorrow, the summary doesn't go stale.

If someone eventually asks for a domain per client, the compose is already
ready for it: adding a Traefik `labels:` is one line. Not implemented now
because nobody asked for it.