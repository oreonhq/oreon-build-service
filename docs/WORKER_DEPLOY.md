# Oreon Build Worker Deployment

## Deploy with RPM (best way)

The worker package is currently available in Oreon 10 repositories.

### 1. Install the worker

```bash
sudo dnf install oreon-build-worker
```

The package creates `/usr/bin/oreon-worker`, the `oreon-worker.service` unit, and config under `/etc/oreon-build-worker/`. It creates the `oreon-build` user.

### 2. Enroll the worker (per instance)

An admin enrolls the worker to get a one-time token.

1. Log in as admin (e.g. http://localhost:8000).
2. Open Workers.
3. In "Enroll worker", set worker name (e.g. `worker-1-x86_64`), enrollment token (`WORKER_ENROLLMENT_SECRET` from controller `.env`), and architecture (e.g. `x86_64`).
4. Click Enroll worker. Copy or save the one-time token somewhere safe because you only see it once.

### 3. Configure the worker

```bash
sudo cp /etc/oreon-build-worker/oreon-worker.env.example /etc/oreon-build-worker/oreon-worker.env
sudo nano /etc/oreon-build-worker/oreon-worker.env
```

Set:

- `CONTROLLER_URL` – e.g. `http://192.168.1.10:8000` (controller API, no trailing slash)
- `OREON_WORKER_TOKEN` – token from step 2
- `OREON_WORKER_NAME` – optional, default `worker-1`
- `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`, `R2_ENDPOINT_URL` – same bucket/creds as the controller (used for build logs only)

Restrict the file: `sudo chmod 600 /etc/oreon-build-worker/oreon-worker.env`

### 4. Start the worker

```bash
sudo systemctl enable --now oreon-worker
```

Also on the worker host:

- `sudo dnf install mock rpmdevtools`
- `sudo usermod -a -G mock oreon-build`

## Cancelling builds

The controller marks the job and running attempts cancelled. Workers poll `GET /api/worker/cancel-check/{attempt_id}` and kill `mock` when a cancel is detected.

## Multiple workers

Give each host a unique `OREON_WORKER_NAME` (worker-1, worker-2, …). Enroll each worker and set its token in that host's `oreon-worker.env`.

## Troubleshooting

- Open an issue if something else breaks.
