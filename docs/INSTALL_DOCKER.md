# Docker Deployment

Docker is the supported deployment method for servers, homelabs, and VPS hosting. The project uses a layered Docker Compose architecture with composable overlays for different scenarios.

Docker deployments use **PostgreSQL** as the application database. The shared `./data` mount handles non-database state (Celery beat schedule, caches).

## Prerequisites

- Docker Engine 20.10+
- Docker Compose v2.18 or newer (`docker compose`); `docker-compose.release.yml` uses the `!reset` tag, which older Compose cannot parse
- Python 3.11+ on the host for `scripts/docker-compose-enabled-markets.sh`; set `STOCKSCREEN_PYTHON=/path/to/python3.11` if your default `python3` is older.

## One-Command Install (Recommended)

```bash
git clone https://github.com/xang1234/stock-screener.git
cd stock-screener
scripts/quickstart.sh
```

`scripts/quickstart.sh`:

- creates `.env.docker` from `.env.docker.example` on first run, with a generated login password and session secret (printed once at the end);
- picks the published GHCR images that match your checkout: the release tag when you cloned one, otherwise that commit's `sha-*` images, otherwise the newest `main` build pinned to its commit's `sha-*` images (so backend and frontend always come from the same build);
- on Linux, lets the container user (uid 1000) write to `./data` with an ACL (`setfacl`, from the `acl` package), so your own ownership and later `git pull` updates keep working; without `setfacl` it falls back to `sudo chown`;
- pulls the images, starts the stack with the production and release overlays, and waits until every service is healthy.

Optional settings, saved to `.env.docker` when given: `ENABLED_MARKETS=US,HK` (default `US`), `FRONTEND_PORT=8080` (default `80`). `APP_IMAGE_TAG=vX.Y.Z` pins a specific image tag. Re-run the script after `git pull` to upgrade; your password and other settings in `.env.docker` are kept. For a fixed release, clone it directly: `git clone --branch vX.Y.Z ...`.

Images are published for `linux/amd64` only. Docker Desktop on Apple Silicon runs them through Rosetta automatically (slower, especially on first start); ARM Linux hosts need amd64 emulation (`qemu-user-static` / binfmt) installed first.

CI runs this exact script against each published `main` build and release tag, and on pull requests that change install files (`quickstart-smoke` in `.github/workflows/ci.yml`). A GitHub Release is only created after it passes.

The rest of this guide covers the same stack configured by hand.

## Quick Start (Local Development)

Zero-config local deployment:

```bash
# 1. Set up environment (required for chatbot/LLM features)
cp .env.docker.example .env
# Edit .env: Set SERVER_AUTH_PASSWORD and add your API keys (GROQ_API_KEY, MINIMAX_API_KEY, etc.)

# 2. Start the local-default stack
scripts/docker-compose-enabled-markets.sh up
```

This starts PostgreSQL, Redis, the Backend API, the shared Celery workers, the market workers selected by `ENABLED_MARKETS`, and the Frontend. Access at **http://localhost**.

> **Note:** Local backups are now opt-in so the default laptop stack stays lighter. Start `db-backup` with `COMPOSE_PROFILES=backup scripts/docker-compose-enabled-markets.sh up -d db-backup` (or add the profile in a local override) when you want local `pg_dump` snapshots under `./data/backups`.

> **Note:** This local quick start reads environment variables from `.env` in the project root. The production examples below pass `--env-file .env.docker` explicitly. `SERVER_AUTH_PASSWORD` is required for server access, and LLM API keys are required for chatbot features.

## Homelab (Behind Reverse Proxy)

For deployment behind Traefik, nginx proxy manager, or similar:

```bash
# 1. Configure environment
cp .env.docker.example .env.docker
# Edit .env.docker: Set SERVER_AUTH_PASSWORD, CORS_ORIGINS=https://stocks.home.lan,
# and SERVER_AUTH_SECURE_COOKIE=true if your proxy terminates HTTPS

# 2. Start with production settings
ENABLED_MARKETS=US,HK,CN scripts/docker-compose-enabled-markets.sh --env-file .env.docker -f docker-compose.yml -f docker-compose.prod.yml up -d

# 3. Configure your reverse proxy to forward to port 80
```

The production overlay adds resource limits, health checks, and JSON logging with rotation.

## Production with GHCR Images (Recommended)

Use pre-built images from GitHub Container Registry instead of building from source on the server:

```bash
# 1. Configure environment
cp .env.docker.example .env.docker
# Edit .env.docker:
#   BACKEND_IMAGE=ghcr.io/xang1234/stockscreenclaude-backend
#   FRONTEND_IMAGE=ghcr.io/xang1234/stockscreenclaude-frontend
#   APP_IMAGE_TAG=vX.Y.Z   (a release tag from https://github.com/xang1234/stock-screener/releases)
#   SERVER_AUTH_PASSWORD=choose-a-long-random-password
#   CORS_ORIGINS=https://stocks.yourdomain.com

# 2. Pull the tagged release images
ENABLED_MARKETS=US,HK,CN scripts/docker-compose-enabled-markets.sh --env-file .env.docker -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.release.yml pull

# 3. Deploy without rebuilding locally
ENABLED_MARKETS=US,HK,CN scripts/docker-compose-enabled-markets.sh --env-file .env.docker -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.release.yml up -d --no-build
```

For HTTPS on a standalone VPS, add the Caddy overlay:
```bash
ENABLED_MARKETS=US,HK,CN scripts/docker-compose-enabled-markets.sh --env-file .env.docker -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.release.yml -f docker-compose.https.yml pull
ENABLED_MARKETS=US,HK,CN scripts/docker-compose-enabled-markets.sh --env-file .env.docker -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.release.yml -f docker-compose.https.yml up -d --no-build
```

### Release and Rollback

- Push to `main` to publish rolling `main`, `sha-*`, and `latest` image tags to GHCR
- Push a git tag like `v1.3.0` to publish immutable release tags
- **Deploy:** Set `APP_IMAGE_TAG=v1.3.0` in `.env.docker`, run `pull` + `up -d --no-build`
- **Roll back:** Change `APP_IMAGE_TAG` to the previous tag and redeploy

If the repository or package is private, authenticate first:
```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u <github-username> --password-stdin
```

## VPS with Auto-HTTPS (Hostinger, DigitalOcean, etc.)

Includes Caddy for automatic Let's Encrypt certificates:

```bash
# 1. Configure environment
cp .env.docker.example .env.docker
# Edit .env.docker: Set DOMAIN=stocks.yourdomain.com
# Edit .env.docker: Set SERVER_AUTH_PASSWORD=choose-a-long-random-password
# Edit .env.docker: Set CORS_ORIGINS=https://stocks.yourdomain.com

# 2. Ensure DNS A record points to your server IP

# 3. Start with HTTPS
ENABLED_MARKETS=US,HK,CN scripts/docker-compose-enabled-markets.sh --env-file .env.docker -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.https.yml up -d
```

Requirements:
- DNS A record pointing to your server
- Ports 80 and 443 open
- `DOMAIN` environment variable set

The HTTPS overlay sets `SERVER_AUTH_SECURE_COOKIE=true` on the backend automatically so auth cookies remain Secure behind Caddy TLS termination.

## Services Architecture

| Service | Purpose |
|---------|---------|
| `redis` | Celery broker (DB 0) and result backend (DB 1) |
| `postgres` | Application database |
| `backend` | FastAPI API server |
| `celery-worker` | General compute queue (2 workers) |
| `celery-datafetch` | Data fetch queue (1 worker, serialized for rate limits; listens only to enabled-market queues) |
| `celery-userscans` | Shared user scan queue |
| `celery-marketjobs-*` | Enabled-market compute queues, selected through Compose profiles |
| `celery-userscans-*` | Enabled-market user scan queues, selected through Compose profiles |
| `celery-beat` | Celery Beat scheduler |
| `db-backup` | Automated PostgreSQL backups to `./data/backups` |
| `frontend` | React app served via nginx |
| `caddy` | (HTTPS overlay only) TLS termination with Let's Encrypt |

## Docker Compose File Reference

| File | Purpose |
|------|---------|
| `docker-compose.yml` | Base configuration for local development |
| `docker-compose.prod.yml` | Production overlay: resource limits, health checks, logging |
| `docker-compose.release.yml` | Release overlay: deploy tagged GHCR images instead of local builds |
| `docker-compose.https.yml` | HTTPS overlay: Caddy with automatic Let's Encrypt |
| `.env.docker.example` | Template for Docker environment variables |
| `Caddyfile` | Caddy configuration for TLS termination |

## PostgreSQL Notes

### Upgrade from Older Versions

The backend runs as non-root user (uid 1000). If upgrading from an older version:
```bash
sudo chown -R 1000:1000 ./data
```

### Backups and Restore

PostgreSQL backups are automatically written by the `db-backup` service via `pg_dump`. Restore with:
```bash
pg_restore -d <database> <dump-file>
```

### Legacy Pre-Alembic Upgrade Path

A database created before Alembic baseline `20260408_0001` and never upgraded since has tables but no `alembic_version` table. Current releases refuse to start against it. Start release v1.6.0 once against the database, then upgrade. Do this in your existing checkout: the database lives in its `./docker-data/postgres` directory, so a fresh clone would start v1.6.0 against an empty database.

1. Start v1.6.0 in the existing checkout:
   - GHCR images: set `APP_IMAGE_TAG` to `v1.6.0` in `.env.docker`, then run `pull` and `up -d --no-build` with the release overlay as in [Production with GHCR Images](#production-with-ghcr-images-recommended).
   - Source builds: run `git checkout v1.6.0`, then rebuild and start the stack with `up -d --build`.
2. Wait until `docker compose logs backend` shows `Database schema ready`. v1.6.0's startup has then reconciled the old schema, stamped the baseline, and upgraded.
3. Return to the current release: set `APP_IMAGE_TAG` back, or `git checkout` your branch again, and deploy as usual. Its startup runs the remaining migrations.

Fresh installs now auto-seed `ibd_industry_groups` from the bundled canonical CSV on backend startup when the table is empty.
If you already have an existing database with an empty `ibd_industry_groups` table, repair it with:
```bash
scripts/docker-compose-enabled-markets.sh --env-file .env.docker -f docker-compose.yml -f docker-compose.prod.yml run --rm backend python scripts/seed_ibd_industry_groups.py
```

## Troubleshooting

### Chatbot not responding
Docker Compose reads API keys from `.env` in the project root. If `.env` is missing or keys are empty, scanning still works but the chatbot won't. Check:
```bash
docker compose exec backend env | grep -i API_KEY
```

### CORS errors in browser
Set `CORS_ORIGINS` in your environment file to match your access URL (e.g., `https://stocks.home.lan`). Restart the backend after changes.

### Permission denied on ./data
The backend container runs as uid 1000. Fix with:
```bash
sudo chown -R 1000:1000 ./data
```

### Container health checks failing
```bash
docker compose ps
docker compose logs backend
docker compose exec backend curl -f http://localhost:8000/readyz
curl -f http://localhost/nginx-health
```
