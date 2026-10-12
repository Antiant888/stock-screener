#!/usr/bin/env bash
# CI: run the documented one-command install against the images just published
# for this commit or release tag, then check a new user can reach and use it.
#
# Required: EXPECTED_IMAGE_TAG, the tag (or bash pattern, e.g. sha-*) quickstart.sh
# must choose for this checkout.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env.docker"
FRONTEND_PORT="${FRONTEND_PORT:-18082}"
BASE_URL="http://127.0.0.1:${FRONTEND_PORT}"
COOKIE_JAR="$(mktemp)"
COMPOSE=(
  "$ROOT_DIR/scripts/docker-compose-enabled-markets.sh" --env-file "$ENV_FILE"
  -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.release.yml
)
: "${EXPECTED_IMAGE_TAG:?set EXPECTED_IMAGE_TAG}"

cleanup() {
  cd "$ROOT_DIR"
  if [[ -f "$ENV_FILE" ]]; then
    "${COMPOSE[@]}" ps -a || true
    "${COMPOSE[@]}" logs --tail=80 || true
    "${COMPOSE[@]}" down -v >/dev/null 2>&1 || true
  fi
  rm -f "$COOKIE_JAR"
}
trap cleanup EXIT

[[ ! -f "$ENV_FILE" ]] || { echo "Refusing to run: $ENV_FILE already exists." >&2; exit 1; }

FRONTEND_PORT="$FRONTEND_PORT" bash "$ROOT_DIR/scripts/quickstart.sh"

env_value() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1; }

chosen="$(env_value APP_IMAGE_TAG)"
# Unquoted on purpose: EXPECTED_IMAGE_TAG may be a pattern such as sha-*.
# shellcheck disable=SC2053
[[ "$chosen" == $EXPECTED_IMAGE_TAG ]] || {
  echo "quickstart chose image tag '$chosen', expected '$EXPECTED_IMAGE_TAG'" >&2
  exit 1
}

index_html="$(curl -fsS "$BASE_URL/")" || { echo "Frontend did not respond" >&2; exit 1; }
[[ "$index_html" == *'<div id="root">'* ]] || { echo "Frontend did not serve the app" >&2; exit 1; }

curl -fsS -c "$COOKIE_JAR" -H 'Content-Type: application/json' \
  -d "{\"password\": \"$(env_value SERVER_AUTH_PASSWORD)\"}" \
  "$BASE_URL/api/v1/auth/login" >/dev/null || { echo "Login with the generated password failed" >&2; exit 1; }

curl -fsS -b "$COOKIE_JAR" "$BASE_URL/api/v1/runtime/bootstrap-status" \
  | python3 -c 'import json, sys; p = json.load(sys.stdin); assert p["bootstrap_required"] is True and p["enabled_markets"] == ["US"], p' \
  || { echo "A fresh install did not offer the first-run bootstrap for US" >&2; exit 1; }

echo "Quickstart smoke passed ($chosen)."
