# Oreon Build Worker Deployment

This guide explains how to run an Oreon build worker. The worker polls the controller for jobs, runs mock builds, and uploads logs and build artifacts to Cloudflare R2.

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
- `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`, `R2_ENDPOINT_URL` – same bucket/creds as the controller so uploads land in one place

Restrict the file: `sudo chmod 600 /etc/oreon-build-worker/oreon-worker.env`

### 4. Start the worker

```bash
sudo systemctl enable --now oreon-worker
```

## Mock (for RPM builds to run in)

Once done, do this on the worker machine:

1. `sudo dnf install mock`
2. `sudo usermod -a -G mock oreon-build`

## Multiple workers

Give each host a unique `OREON_WORKER_NAME` (worker-1, worker-2, ...). Enroll each worker and set its token in that host's `oreon-worker.env`. You can run multiple workers on one machine (separate units and env files) or one worker per machine. Also, note that by default, the controller can run 3 (or so) builds at a time on one worker to keep the amount of workers needed low, which is great for limited or cost-sensitive infra. You of course can lower this limit in the worker env.

## Troubleshooting

Feel free to open an issue if you face any issues.