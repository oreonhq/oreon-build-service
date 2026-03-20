# Oreon Build Service

Super powerful, lightweight, and easy to deploy RPM build system with CVE tracking. 

## Why another build system?

We were intially going to use Koji for our build system as we are planning to go independent from upstream repos starting in Oreon 11, but Koji is designed to run on large infrastructure, and deploying it is a huge pain, so we wanted to make the painless build system that can run on almost any server. We don't store repos or artifacts locally, everything goes straight to Cloudflare R2 and workers can run multiple tasks at once, making it perfect for smaller projects or cost sensitive setups.

## Features

- Packages and builds: Ingest sources/specs, resolve deps, schedule builds, build RPMs in mock
- R2 for everything: Repos and artifacts go straight to Cloudflare R2. No local repo storage. Paths: `<bucket>/<releasename>/<channel>/<basearch>/` plus `<bucket>/<releasename>/src/` for source RPMs
- Releases: Releasename, arches, base repos, channels
- Workers: Enroll with a token, poll for jobs, upload build logs to R2, POST built RPMs to the controller (controller signs and stores in R2). States: idle, busy, unhealthy, offline, draining
- Scheduling: Cron-style jobs, dependency rebuilds, compose
- Git: Pull sources, trigger builds
- Signing: RPM and repodata via GPG on the controller
- Web UI: Read-only for guests, log in needed to trigger builds, create stuff, enroll workers
- Accounts: Admin and Maintainer. Default admin comes from `.env`

## Components

Here’s what you deploy:

- API server (FastAPI): packages, builds, releases, workers, mock envs, promotions, repos, schedules, audit, worker poll/heartbeat/result
- Worker: polls controller, runs mock builds, uploads logs to R2, sends RPMs to controller for signing and R2 upload
- Scheduler: runs scheduled tasks (nightly builds, compose)
- Publisher: composes repos with `createrepo_c` and uploads to R2 (no local repo storage)
- CLI: `oreon-buildctl` (alternative to the web interface)

## Requirements

- Python 3.11+
- PostgreSQL
- Cloudflare R2 for all repo/artifact storage
- Controller host `createrepo_c`, GPG signing key + `rpm-sign` for RPM/repodata signing (see `.env.example`)
- Workers `mock`, `rpmdevtools` no GPG or `rpm-sign` on workers for artifact signing
- Optional: `MAX_WORKER_RPM_UPLOAD_MIB` in controller `.env` if workers build very large RPMs (default 4096 MiB; HTTP 413 means increase this)

NOTE: we ship `psycopg2-binary` for Alembic (sync). The app talks to Postgres with `asyncpg` at runtime, so `DATABASE_URL` should use `postgresql+asyncpg://`.

## Deploy script (quickest way to deploy)

this is an all in one deploy script :)

```bash
./scripts/deploy.sh
```

You can set `OREON_DB_PASSWORD` first if you want a specific DB password; otherwise the script generates one and writes it to `.env`. Then start the API (step 4 below).

## Repository layout (R2)

- Binary: `<bucket>/<releasename>/<channel>/<basearch>/RPMS/`, `repodata/`
- Source: `<bucket>/<releasename>/src/*.src.rpm`
- Logs: `<releasename>/logs/<attempt_id>.log`

## Deployment

Systemd unit examples live in `deploy/` when you need them.

also: [Worker deploy guide](docs/WORKER_DEPLOY.md) (you will need workers for builds to actually build)

## Security

- Web/API auth is JWT and current roles are admin and maintainer
- Workers use a per-worker token (you get it when you enroll)
- Actions are audit-logged

## License

GPLv3. See [LICENSE](LICENSE).