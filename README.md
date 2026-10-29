# Oreon Build Service
An RPM build system for Oreon.

Oreon Build Service: https://build.oreonhq.com

## Running it
You need Python 3.11+, PostgreSQL, mock and createrepo_c. (other deps may be needed depending on setup)

```
./scripts/deploy.sh
./start.sh
```

API will be reachable via `http://localhost:8000`. Run `./stop.sh` or `./start.sh` (self explanatory).

For setting up build workers, see [docs/WORKER_DEPLOY.md](docs/WORKER_DEPLOY.md).

## License
GPLv3, see [LICENSE](LICENSE).
