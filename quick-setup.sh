#!/usr/bin/env bash
# quick-setup.sh — one-time developer setup: env file, API key, and local
# dependencies for backend (uv) and frontend (npm). Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\033[1;36m▸ %s\033[0m\n' "$*"; }
# portable lowercase-compare — macOS ships bash 3.2 (no ${var,,})
is_yes() { case "$1" in [yY]|[yY][eE][sS]) return 0 ;; *) return 1 ;; esac; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }

# ── flags ────────────────────────────────────────────────────────
usage() {
  cat <<'EOF'
quick-setup.sh — one-time developer setup: .env, provider API keys
(verified before saving), optional Redis, and local dev dependencies.
Safe to re-run any time; run with no flags for the interactive walkthrough.

USAGE
  ./quick-setup.sh [options]

INTERACTIVE (no flags)
  1. Creates .env from .env.example if missing.
  2. Asks which model provider(s) to configure:
       1) Anthropic            2) Google              3) OpenAI
       4) Anthropic + Google   5) Anthropic + OpenAI  6) Google + OpenAI
       7) All three            8) None - keyless demo mode (fake provider)
  3. Prompts for each selected provider's API key (hidden input; offers
     to replace an existing key), then VERIFIES it with a free
     list-models API call. A rejected or unreachable key warns and asks
     "Save it anyway? [y/N]".
  4. Asks whether to provision the optional Redis cache backend
     (usage stays a runtime decision in Settings -> Registry cache).
  5. Installs backend (uv sync) and frontend (npm install) dev deps.

RE-RUNNING = UPDATING
  The script is safe to re-run at any time, and every prompt defaults to
  "keep what I have" — hit Enter all the way through and nothing changes:
    - the provider menu pre-selects the combination you already have keys
      for ("[N = your current setup]")
    - an existing key shows its last 4 characters and Enter keeps it;
      answer y and paste to replace just that one key (verified as usual)
    - existing Redis provisioning is kept on Enter ("Keep it? [Y/n]")
    - a leftover FAKE_LLM_ENABLED=1 is kept on Enter; answer y to disable
  Example: to rotate only your Anthropic key a month later, re-run, hit
  Enter at the menu, answer y at the Anthropic "Replace it?" prompt,
  paste the new key, and Enter through everything else.

NON-INTERACTIVE OPTIONS
  --providers LIST      Comma list (no spaces): anthropic,google,openai
                        or the shorthands  all | none
                        'none' enables keyless demo mode (FAKE_LLM_ENABLED=1)
  --anthropic-key KEY   Set the Anthropic key (implies its provider)
  --google-key KEY      Set the Google key (implies its provider)
  --openai-key KEY      Set the OpenAI key (implies its provider)
  --key KEY             Legacy alias for --anthropic-key
  --redis               Provision the Redis cache backend without asking
  --no-redis            Skip Redis without asking
  -h, --help            Show this help and exit

  Keys passed via flags are still verified; on failure they are saved
  anyway (you passed them explicitly) with a clear warning.

EXAMPLES
  ./quick-setup.sh
  ./quick-setup.sh --providers all --no-redis
  ./quick-setup.sh --anthropic-key sk-ant-... --google-key AIza... --redis
  ./quick-setup.sh --providers none        # keyless demo mode
  CI: ./quick-setup.sh --providers openai --openai-key "$OPENAI_API_KEY" --no-redis

WHAT HAPPENS NEXT
  Only providers with a key appear in the UI's model selects; first boot
  picks default_model from whatever is configured (anthropic sonnet ->
  gemini flash -> gpt-5.6 luna -> fake). Then: ./build.sh && ./start.sh
EOF
}

# --providers anthropic,google,openai|all|none   (comma list, no spaces)
# --anthropic-key K   --google-key K   --openai-key K   (imply their provider)
# --key K             (back-compat alias for --anthropic-key)
# --redis / --no-redis     -h/--help
PROVIDERS=""
KEY_anthropic=""; KEY_google=""; KEY_openai=""
REDIS_CHOICE=""

die() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

add_provider() { # $1 = provider id; append if not already listed
  case ",$PROVIDERS," in *",$1,"*) ;; *) PROVIDERS="${PROVIDERS:+$PROVIDERS,}$1" ;; esac
}

need_val() { # $1 = flag name, $2 = value (must exist and not be another flag)
  [ -n "${2:-}" ] || die "$1 needs a value (see ./quick-setup.sh --help)"
  case "$2" in --*) die "$1 needs a value, got '$2' (see ./quick-setup.sh --help)" ;; esac
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)       usage; exit 0 ;;
    --providers)     need_val "$1" "${2:-}"; PROVIDERS="$2"; shift 2 ;;
    --anthropic-key) need_val "$1" "${2:-}"; KEY_anthropic="$2"; add_provider anthropic; shift 2 ;;
    --google-key)    need_val "$1" "${2:-}"; KEY_google="$2";    add_provider google;    shift 2 ;;
    --openai-key)    need_val "$1" "${2:-}"; KEY_openai="$2";    add_provider openai;    shift 2 ;;
    --key)           need_val "$1" "${2:-}"; KEY_anthropic="$2"; add_provider anthropic; shift 2 ;;
    --redis)         REDIS_CHOICE="yes"; shift ;;
    --no-redis)      REDIS_CHOICE="no";  shift ;;
    *) die "unknown option: $1 (see ./quick-setup.sh --help)" ;;
  esac
done
[ "$PROVIDERS" = "all" ] && PROVIDERS="anthropic,google,openai"

# keyless mode and key flags are mutually exclusive, whatever the flag order
if [ "$PROVIDERS" = "none" ] && { [ -n "$KEY_anthropic" ] || [ -n "$KEY_google" ] || [ -n "$KEY_openai" ]; }; then
  die "--providers none (keyless mode) cannot be combined with key flags"
fi

# validate the provider list before touching anything — a typo must fail
# loudly, never configure nothing
if [ -n "$PROVIDERS" ] && [ "$PROVIDERS" != "none" ]; then
  OLDIFS="$IFS"; IFS=','
  for p in $PROVIDERS; do
    case "$p" in
      anthropic|google|openai) ;;
      none) die "--providers 'none' cannot be combined with other providers or key flags" ;;
      '') die "--providers has an empty entry (check for stray commas): '$PROVIDERS'" ;;
      *) die "unknown provider '$p' — valid: anthropic, google, openai, all, none" ;;
    esac
  done
  IFS="$OLDIFS"
fi

# ── 1. .env ──────────────────────────────────────────────────────
if [ ! -f .env ]; then
  [ -f .env.example ] || die ".env.example not found — run this script from a complete checkout of the repo root"
  cp .env.example .env
  say "created .env from .env.example"
else
  say ".env already present"
fi

set_env_line() { # $1 = KEY, $2 = value ('' removes the line)
  grep -vE "^$1=" .env > .env.tmp || true
  [ -n "$2" ] && printf '%s=%s\n' "$1" "$2" >> .env.tmp
  mv .env.tmp .env
}

# ── 2. model providers ───────────────────────────────────────────
# Any combination works; only providers with a key show up in the UI's
# model selects. First boot picks the default model from whatever is
# configured (anthropic → gemini flash → gpt-5.6 luna → fake).

# Re-runs are updates: the menu defaults to the combination you already
# have keys for, so Enter keeps your current setup and only the prompts
# you answer differently change anything.
detect_current_choice() {
  has_a=""; has_g=""; has_o=""
  grep -qE '^ANTHROPIC_API_KEY=.+' .env && has_a=1
  grep -qE '^GOOGLE_API_KEY=.+' .env && has_g=1
  grep -qE '^OPENAI_API_KEY=.+' .env && has_o=1
  case "${has_a:-0}${has_g:-0}${has_o:-0}" in
    100) printf 1 ;; 010) printf 2 ;; 001) printf 3 ;;
    110) printf 4 ;; 101) printf 5 ;; 011) printf 6 ;;
    111) printf 7 ;;
    *) if grep -qE '^FAKE_LLM_ENABLED=1' .env; then printf 8; else printf 1; fi ;;
  esac
}

if [ -z "$PROVIDERS" ] && [ -t 0 ]; then
  default_choice="$(detect_current_choice)"
  say "Which model provider(s) do you want to configure?"
  printf '  1) Anthropic            2) Google              3) OpenAI\n'
  printf '  4) Anthropic + Google   5) Anthropic + OpenAI  6) Google + OpenAI\n'
  printf '  7) All three            8) None - keyless demo mode (fake provider)\n'
  if [ "$default_choice" != "1" ] || grep -qE '^ANTHROPIC_API_KEY=.+' .env; then
    printf 'Choice [%s = your current setup]: ' "$default_choice"
  else
    printf 'Choice [%s]: ' "$default_choice"
  fi
  read -r choice
  case "${choice:-$default_choice}" in
    1) PROVIDERS="anthropic" ;;
    2) PROVIDERS="google" ;;
    3) PROVIDERS="openai" ;;
    4) PROVIDERS="anthropic,google" ;;
    5) PROVIDERS="anthropic,openai" ;;
    6) PROVIDERS="google,openai" ;;
    7) PROVIDERS="anthropic,google,openai" ;;
    8) PROVIDERS="none" ;;
    *) warn "unrecognized choice '$choice' — keeping current setup"; PROVIDERS="skip" ;;
  esac
elif [ -z "$PROVIDERS" ]; then
  say "non-interactive run with no --providers — leaving provider keys unchanged"
  PROVIDERS="skip"
fi

env_key_name() { # provider id → .env variable name
  case "$1" in
    anthropic) printf 'ANTHROPIC_API_KEY' ;;
    google)    printf 'GOOGLE_API_KEY' ;;
    openai)    printf 'OPENAI_API_KEY' ;;
  esac
}

verify_key() { # $1 = provider, $2 = key → 0 if the key works (cheap list-models call)
  command -v curl >/dev/null 2>&1 || { warn "curl not found — skipping key verification"; return 0; }
  code=""
  case "$1" in
    anthropic)
      code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 \
        -H "x-api-key: $2" -H "anthropic-version: 2023-06-01" \
        https://api.anthropic.com/v1/models 2>/dev/null || printf '000')" ;;
    google)
      code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 \
        "https://generativelanguage.googleapis.com/v1beta/models?key=$2" 2>/dev/null || printf '000')" ;;
    openai)
      code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 \
        -H "Authorization: Bearer $2" \
        https://api.openai.com/v1/models 2>/dev/null || printf '000')" ;;
  esac
  if [ "$code" = "200" ]; then
    say "$1 key verified (list-models call succeeded)"
    return 0
  fi
  case "$code" in
    000) warn "$1 key could not be verified — network unreachable or timeout" ;;
    400|401|403) warn "$1 key REJECTED by the API (HTTP $code) — it looks invalid" ;;
    *) warn "$1 key verification returned HTTP $code" ;;
  esac
  return 1
}

save_key() { # $1 = provider, $2 = key
  set_env_line "$(env_key_name "$1")" "$2"
  say "$(env_key_name "$1") written to .env (never commit this file)"
}

configure_provider() { # $1 = provider id, $2 = key from flags ('' = prompt)
  var="$(env_key_name "$1")"
  # non-empty match only: a stray empty '$var=' line (hand-edited .env)
  # must not shadow a real key further down
  current="$(grep -E "^$var=.+" .env | head -1 | cut -d= -f2- || true)"
  new_key="$2"

  if [ -z "$new_key" ] && [ -t 0 ]; then
    if [ -n "$current" ]; then
      key_tail="$(printf '%s' "$current" | tail -c 4)"
      printf 'A %s is already set (…%s). Replace it? [y/N] ' "$var" "$key_tail"
      read -r replace
      is_yes "$replace" || { say "keeping the existing $1 key"; return 0; }
    fi
    printf 'Paste your %s (input hidden, Enter to skip): ' "$var"
    read -rs new_key; echo
    [ -n "$new_key" ] || { warn "no $1 key provided — re-run ./quick-setup.sh to add it later"; return 0; }
  elif [ -z "$new_key" ]; then
    if [ -n "$current" ]; then say "$var already set — keeping it (non-interactive run)"
    else warn "no TTY and no $1 key — pass one with --$1-key"; fi
    return 0
  fi

  if verify_key "$1" "$new_key"; then
    save_key "$1" "$new_key"
  elif [ -t 0 ]; then
    printf 'Save the unverified %s key anyway? [y/N] ' "$1"
    read -r anyway
    if is_yes "$anyway"; then save_key "$1" "$new_key"; else warn "$1 key NOT saved"; fi
  else
    warn "saving the $1 key anyway (non-interactive run — it was passed explicitly)"
    save_key "$1" "$new_key"
  fi
}

contains_provider() { case ",$PROVIDERS," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }

if [ "$PROVIDERS" = "none" ]; then
  set_env_line FAKE_LLM_ENABLED "1"
  say "keyless demo mode: FAKE_LLM_ENABLED=1 — first boot will select the"
  say "fake:scripted model; add real keys later by re-running ./quick-setup.sh"
elif [ "$PROVIDERS" != "skip" ]; then
  contains_provider anthropic && configure_provider anthropic "$KEY_anthropic"
  contains_provider google    && configure_provider google    "$KEY_google"
  contains_provider openai    && configure_provider openai    "$KEY_openai"
  # keyless demo flag left over from an earlier setup? ask, don't assume —
  # and Enter keeps it, like every other prompt (re-runs are no-ops by default)
  if grep -qE '^FAKE_LLM_ENABLED=1' .env; then
    if [ -t 0 ]; then
      printf 'FAKE_LLM_ENABLED=1 is set (keyless demo mode). Disable it? [y/N] '
      read -r drop_fake
      if is_yes "$drop_fake"; then
        set_env_line FAKE_LLM_ENABLED ""
        say "keyless demo mode disabled"
      else
        say "keeping FAKE_LLM_ENABLED=1 (fake provider stays available)"
      fi
    else
      say "FAKE_LLM_ENABLED=1 left as-is (non-interactive run)"
    fi
  fi
fi

# ── 2b. optional Redis (registry-cache backend, spec §7.3) ───────
# Provisioning is upfront; USAGE stays a runtime decision — the cache mode
# defaults to 'bypass' and is flipped in Settings → Registry cache later.

set_redis_lines() { # $1 = REDIS_URL value, $2 = COMPOSE_PROFILES value
  # both lines in one temp-file pass + one mv: an interrupt can never
  # leave half a provisioning behind
  grep -vE "^(REDIS_URL|COMPOSE_PROFILES)=" .env > .env.tmp || true
  [ -n "$1" ] && printf 'REDIS_URL=%s\n' "$1" >> .env.tmp
  [ -n "$2" ] && printf 'COMPOSE_PROFILES=%s\n' "$2" >> .env.tmp
  mv .env.tmp .env
}

setup_redis() {
  set_redis_lines "redis://redis:6379/0" "redis"
  say "Redis provisioned: ./start.sh will now include the redis service"
  say "(cache mode stays 'bypass' until you flip it in Settings → Registry cache)"
}

skip_redis() {
  set_redis_lines "" ""
  say "Redis not provisioned — memory/bypass cache modes remain available"
}

if [ "$REDIS_CHOICE" = "yes" ]; then
  setup_redis
elif [ "$REDIS_CHOICE" = "no" ]; then
  skip_redis
elif [ -t 0 ]; then
  if grep -qE '^REDIS_URL=.+' .env; then
    # already provisioned — Enter keeps it (re-runs are updates)
    printf 'Redis is currently provisioned. Keep it? [Y/n] '
    read -r keep_redis
    case "$keep_redis" in
      [nN]*) skip_redis ;;
      *) say "keeping the existing Redis provisioning" ;;
    esac
  else
    printf 'Set up Redis as an optional registry-cache backend? [y/N] '
    read -r want_redis
    if is_yes "$want_redis"; then setup_redis; else skip_redis; fi
  fi
else
  say "non-interactive run — Redis unchanged (use --redis / --no-redis)"
fi

# ── 3. backend dependencies (local dev; docker builds are separate) ──
if command -v uv >/dev/null 2>&1; then
  say "installing backend dependencies (uv sync)…"
  (cd backend && uv sync)
else
  warn "uv not found — skipping local backend deps (install: https://docs.astral.sh/uv/)"
  warn "docker builds via ./build.sh do not need local uv."
fi

# ── 4. frontend dependencies ─────────────────────────────────────
if command -v npm >/dev/null 2>&1; then
  say "installing frontend dependencies (npm install)…"
  (cd frontend && npm install)
else
  warn "npm not found — skipping local frontend deps (install Node 22+)"
  warn "docker builds via ./build.sh do not need local npm."
fi

# ── 5. configuration summary ─────────────────────────────────────
# Never end silently: state exactly what is (and is not) configured.
summary_provider() { # $1 = provider id, $2 = env var
  val="$(grep -E "^$2=" .env | head -1 | cut -d= -f2- || true)"
  if [ -n "$val" ]; then
    say "  $1: key set (…$(printf '%s' "$val" | tail -c 4))"
    any_provider=1
  else
    say "  $1: no key"
  fi
}

say "── configuration summary ──"
any_provider=""
summary_provider anthropic ANTHROPIC_API_KEY
summary_provider google GOOGLE_API_KEY
summary_provider openai OPENAI_API_KEY
if grep -qE '^FAKE_LLM_ENABLED=1' .env; then
  say "  keyless demo mode: ON (fake provider available)"
  any_provider=1
else
  say "  keyless demo mode: off"
fi
if grep -qE '^REDIS_URL=.+' .env; then
  say "  redis cache backend: provisioned"
else
  say "  redis cache backend: not provisioned (bypass/memory modes available)"
fi
if [ -z "$any_provider" ]; then
  warn "NO provider configured and keyless mode is off — the app will start,"
  warn "but runs cannot execute until you re-run ./quick-setup.sh with a key"
  warn "or choose keyless demo mode (--providers none)."
fi

say "setup complete. Next: ./build.sh (build images) then ./start.sh (run the stack)."
