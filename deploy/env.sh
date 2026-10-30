# Sourced by the deploy scripts. Reads DEPLOY_HOST and DOMAIN from the shell, or else from .env.
# Only these two keys are read; the secrets in .env are not loaded into the shell.

env_get() {
  [ -f .env ] && grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- | sed -E 's/^"(.*)"$/\1/' || true
}

HOST="${DEPLOY_HOST:-$(env_get DEPLOY_HOST)}"
DOMAIN="${DOMAIN:-$(env_get DOMAIN)}"
if [ -z "$HOST" ]; then
  echo "DEPLOY_HOST is not set: add DEPLOY_HOST=user@server to .env (see .env.example)" >&2
  exit 1
fi
