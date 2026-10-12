#!/usr/bin/env bash
# One-command self-hosted install. Run from a clone of this repository:
#
#   scripts/quickstart.sh
#
# First run writes .env.docker from .env.docker.example with a generated login
# password. Every run pulls the published images that match this checkout,
# starts the stack and waits until all services are healthy. Re-running after
# `git pull` upgrades in place; secrets and settings in .env.docker are kept.
#
# Optional environment:
#   APP_IMAGE_TAG     image tag to run (default: this checkout's release tag or commit)
#   ENABLED_MARKETS   e.g. US,HK (saved to .env.docker; default US)
#   FRONTEND_PORT     host port for the web app (saved to .env.docker; default 80)
#   IMAGE_OWNER       GHCR owner of the images (default xang1234)
#
# STOCKSCREEN_ENV_FILE exists for tests only: containers always load
# ./.env.docker (env_file in docker-compose.prod.yml).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${STOCKSCREEN_ENV_FILE:-$ROOT_DIR/.env.docker}"
IMAGE_OWNER="${IMAGE_OWNER:-xang1234}"
BACKEND_IMAGE="ghcr.io/$IMAGE_OWNER/stockscreenclaude-backend"
FRONTEND_IMAGE="ghcr.io/$IMAGE_OWNER/stockscreenclaude-frontend"
WAIT_TIMEOUT="${QUICKSTART_WAIT_TIMEOUT:-900}"
PYTHON_BIN="${STOCKSCREEN_PYTHON:-python3}"

fail() { echo "quickstart: $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "Docker is not installed. Install Docker Desktop or Docker Engine first: https://docs.docker.com/get-docker/"
docker compose version >/dev/null 2>&1 || fail "the Docker Compose v2 plugin ('docker compose') is required."
docker info >/dev/null 2>&1 || fail "Docker is installed but not running. Start Docker and re-run."
command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "Python 3.11+ is required (set STOCKSCREEN_PYTHON to use a specific interpreter)."
# `up --wait-timeout` arrived in Compose 2.17.
"$PYTHON_BIN" -c 'import re, sys; v = tuple(map(int, re.findall(r"\d+", sys.argv[1])[:2])); sys.exit(v < (2, 17))' \
  "$(docker compose version --short 2>/dev/null || echo 0)" \
  || fail "Docker Compose 2.17 or newer is required (found: $(docker compose version --short 2>/dev/null)). Update Docker."

# Images must match the compose files in this checkout: a release tag when HEAD
# is one, else the commit's CI image, else the newest main build.
resolve_image_tag() {
  local tag sha
  if tag="$(git -C "$ROOT_DIR" describe --tags --exact-match --match 'v*' HEAD 2>/dev/null)"; then
    echo "$tag"
    return
  fi
  # Both images: CI pushes the backend before building the frontend.
  if sha="$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null)" \
    && docker manifest inspect "$BACKEND_IMAGE:sha-${sha:0:7}" >/dev/null 2>&1 \
    && docker manifest inspect "$FRONTEND_IMAGE:sha-${sha:0:7}" >/dev/null 2>&1; then
    echo "sha-${sha:0:7}"
    return
  fi
  echo "quickstart: no published image for this checkout; using 'latest' (newest main build)." >&2
  echo "quickstart: for a pinned release, clone a tag: git clone --branch vX.Y.Z ..." >&2
  echo latest
}

TAG="${APP_IMAGE_TAG:-$(resolve_image_tag)}"

[[ -f "$ENV_FILE" ]] || cp "$ROOT_DIR/.env.docker.example" "$ENV_FILE"

# Set KEY=value lines in place (appending missing keys). Keys passed as
# KEY?=value are only filled when currently empty. Prints the generated password.
NEW_PASSWORD="$("$PYTHON_BIN" - "$ENV_FILE" \
  "BACKEND_IMAGE=$BACKEND_IMAGE" \
  "FRONTEND_IMAGE=$FRONTEND_IMAGE" \
  "APP_IMAGE_TAG=$TAG" \
  ${ENABLED_MARKETS:+"ENABLED_MARKETS=$ENABLED_MARKETS"} \
  ${FRONTEND_PORT:+"FRONTEND_PORT=$FRONTEND_PORT"} \
  "SERVER_AUTH_PASSWORD?=" \
  "SERVER_AUTH_SESSION_SECRET?=" <<'PY'
import secrets
import sys
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()
generated = {
    "SERVER_AUTH_PASSWORD": secrets.token_urlsafe(18),
    "SERVER_AUTH_SESSION_SECRET": secrets.token_urlsafe(48),
}
for arg in sys.argv[2:]:
    key, value = arg.split("=", 1)
    only_if_empty = key.endswith("?")
    key = key.rstrip("?")
    # Compose uses the last occurrence of a key, so edit that one.
    matches = [i for i, line in enumerate(lines) if line.split("=", 1)[0].strip() == key]
    index = matches[-1] if matches else None
    current = lines[index].split("=", 1)[1].strip().strip("'\"") if index is not None else ""
    if only_if_empty:
        if current:
            continue
        value = generated[key]
        if key == "SERVER_AUTH_PASSWORD":
            print(value)
    if index is None:
        lines.append(f"{key}={value}")
    else:
        lines[index] = f"{key}={value}"
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
)"
chmod 600 "$ENV_FILE"

# The backend runs as uid 1000 and writes to the ./data bind mount. Docker
# Desktop maps ownership for you; Linux does not. data/ holds tracked files,
# so grant access with an ACL and keep the user's ownership (a chown would
# stop the next `git pull` from updating them).
if [[ "$(uname -s)" == "Linux" && "$(stat -c %u "$ROOT_DIR/data")" != "1000" ]]; then
  echo "Letting the container user (uid 1000) write to ./data..."
  if command -v setfacl >/dev/null 2>&1; then
    setfacl -R -m u:1000:rwX -m d:u:1000:rwX "$ROOT_DIR/data"
  else
    echo "quickstart: setfacl not found (install the 'acl' package to avoid this); changing ./data ownership to uid 1000 instead." >&2
    echo "quickstart: later updates to tracked data/ files will then need sudo." >&2
    if [[ "$(id -u)" == "0" ]]; then chown -R 1000:1000 "$ROOT_DIR/data"; else sudo chown -R 1000:1000 "$ROOT_DIR/data"; fi
  fi
fi

COMPOSE=(
  "$ROOT_DIR/scripts/docker-compose-enabled-markets.sh" --env-file "$ENV_FILE"
  -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.release.yml
)
cd "$ROOT_DIR"
echo "Pulling images ($TAG)..."
"${COMPOSE[@]}" pull
echo "Starting the stack and waiting for every service to be healthy (up to ${WAIT_TIMEOUT}s)..."
"${COMPOSE[@]}" up -d --no-build --wait --wait-timeout "$WAIT_TIMEOUT"

PORT="$(sed -n 's/^FRONTEND_PORT=//p' "$ENV_FILE" | tail -n 1)"
URL="http://localhost"
[[ -z "$PORT" || "$PORT" == "80" ]] || URL="$URL:$PORT"

echo
echo "StockScreen is running ($TAG):"
echo "$URL"
if [[ -n "$NEW_PASSWORD" ]]; then
  echo "Login password: $NEW_PASSWORD  (saved in $ENV_FILE as SERVER_AUTH_PASSWORD)"
else
  echo "Login password: SERVER_AUTH_PASSWORD in $ENV_FILE"
fi
echo "First launch opens a setup screen: pick a market and start the data bootstrap."
echo "Stop:    ${COMPOSE[*]} down"
