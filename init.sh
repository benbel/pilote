#!/usr/bin/env bash
set -euo pipefail
trap 'echo "init.sh : échec ligne $LINENO" >&2' ERR

main() {
  quiet() {
    local out
    out=$("$@" 2>&1) || {
      printf '%s\n' "$out" | tail -n 5 >&2
      return 1
      }
    }
  die() {
    echo "init.sh : $*" >&2
    exit 1
    }

  [ -n "${GIT_PERSONAL_ACCESS_TOKEN:-}" ] || die "token GitHub absent (Mon compte → Git)"
  git config --global credential.https://github.com.helper \
    '!f() { echo username=x-access-token; echo "password=$GIT_PERSONAL_ACCESS_TOKEN"; }; f'

  if [ -n "${GIT_REPOSITORY:-}" ]; then
    local dir
    dir="$HOME/work/$(basename "$GIT_REPOSITORY" .git)"
    [ -d "$dir/.git" ] || quiet git clone "$GIT_REPOSITORY" "$dir" || die "clone de $GIT_REPOSITORY"
  fi

  [ -n "${VAULT_ADDR:-}" ] && [ -n "${VAULT_TOKEN:-}" ] || die "Vault absent"
  local secret
  secret=$(
    curl -fsS -H "X-Vault-Token: $VAULT_TOKEN" "$VAULT_ADDR/v1/$VAULT_MOUNT/data/$VAULT_TOP_DIR/albert" \
      | python3 -c 'import sys, json; print(json.load(sys.stdin)["data"]["data"]["ALBERT_API_KEY"].strip())' 2>/dev/null
    ) || die "clé Albert (secret albert, variable ALBERT_API_KEY)"
  [ -n "$secret" ] || die "clé Albert vide"
  (
    umask 077
    printf '%s\n' "$secret" > "$HOME/.albert"
    )

  export NVM_DIR="$HOME/.nvm"
  [ -s "$NVM_DIR/nvm.sh" ] \
    || quiet bash -c 'curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | PROFILE=/dev/null bash' \
    || die "nvm"
  set +eu
  . "$NVM_DIR/nvm.sh"
  quiet nvm install 22 && quiet nvm alias default 22 && nvm use default >/dev/null || die "Node"
  set -eu
  quiet npm install -g --ignore-scripts --no-fund --no-audit @earendil-works/pi-coding-agent || die "Pi"

  mkdir -p "$HOME/.pi/agent"
  cat > "$HOME/.pi/agent/models.json" <<'JSON'
{ "providers": { "albert": {
    "baseUrl": "https://albert.api.etalab.gouv.fr/v1",
    "api": "openai-completions",
    "apiKey": "$ALBERT_API_KEY",
    "models": [
      { "id": "deepseek-v4-flash", "contextWindow": 131072 },
      { "id": "gpt-oss-120b", "contextWindow": 131072 }
      ]
    } } }
JSON
  echo '{ "defaultProvider": "albert", "defaultModel": "deepseek-v4-flash" }' > "$HOME/.pi/agent/settings.json"

  grep -q 'ALBERT_API_KEY=' "$HOME/.bashrc" 2>/dev/null || cat >> "$HOME/.bashrc" <<'SH'
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
export ALBERT_API_KEY="$(cat "$HOME/.albert")"
SH
  }

main </dev/null
