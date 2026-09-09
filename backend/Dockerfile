# Backend image: Python 3.12 + uv (uvx for stdio MCP servers) + Node (npx
# for the seeded filesystem MCP server). Single FastAPI process — runs are
# asyncio tasks, no broker/queue (spec §2).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-hashes -o /tmp/requirements.txt \
    && uv pip install --system -r /tmp/requirements.txt

COPY . .

# seed-document gate (spec §13): every .skill.md / .agent.md must be valid
# BEFORE the image ships. Without this a malformed document only surfaces at
# boot — a skill that never loads, or an agent row stuck at status='error'.
# Pure offline validation: no DB, no API keys, no network.
RUN python -m app.doclint

# pre-warm the seeded MCP servers into local caches so first connect is fast
# and works without registry access at runtime
# This prewarm must SUCCEED, not silently pass. With `|| true` a network
# blip cached a no-op layer, and every later build then failed at the
# verification gate below with a misleading "network?" message — the real
# cause (a poisoned cache layer) was invisible and survived rebuilds until
# someone busted the cache by hand. Fail here, where the cause is.
RUN uvx --with "mcp<2" mcp-server-fetch --help >/dev/null 2>&1 \
    && find /root/.cache/uv -type d -path '*readabilipy/javascript' | grep -q . \
    || { echo "BUILD ERROR: mcp-server-fetch prewarm failed — readabilipy is not in the uv cache. Check network/proxy access to PyPI, then rebuild."; exit 1; }
# genuinely optional: a cold npx here only makes the first filesystem-server
# spawn slower (its output goes to stderr, so it cannot corrupt JSON-RPC)
RUN npx -y @modelcontextprotocol/server-filesystem --help >/dev/null 2>&1 || true

# readabilipy (inside mcp-server-fetch's uv env) shells out to node and runs
# `npm install` in its javascript dir on first use — over the stdio transport
# that leaks npm/node output into the JSON-RPC channel at request time, and
# npm-latest undici/whatwg-url require node >=22 while this image ships 20
# (EBADENGINE warns but installs anyway, then crashes at require-time).
# Install at build time with undici pinned to 6.x; jsdom's nested copy must
# go so resolution falls back to the hoisted 6.x. Every step fails the build
# loudly — a silent skip here ships a broken fetch tool.
# Fix EVERY cached copy — tool envs under archive-v0 AND the wheel cache
# (wheels-v6). uvx rebuilds tool environments by copying packages from the
# wheel cache, so a fix baked only into the current env is lost the next
# time uvx constructs a fresh env (seen after a long container suspension):
# the new copy has no node_modules and readabilipy re-runs `npm install` at
# request time, pulling node>=22-only undici 8 again.
RUN dirs=$(find /root/.cache/uv -type d -path '*readabilipy/javascript'); \
    test -n "$dirs" || { echo "BUILD ERROR: readabilipy not found — the uvx mcp-server-fetch prewarm above must have failed (network?)"; exit 1; }; \
    for dir in $dirs; do \
      cd "$dir" \
      && npm install \
      && npm install undici@6.21.3 --no-save \
      && rm -rf node_modules/jsdom/node_modules/undici || exit 1; \
    done
# build-time proof: EVERY cached copy resolves undici 6.x and extracts
RUN for dir in $(find /root/.cache/uv -type d -path '*readabilipy/javascript'); do \
      cd "$dir" \
      && node -e "const v=require('undici/package.json').version; if(!v.startsWith('6.')) {console.error('BUILD ERROR: undici '+v+' resolved in $dir, need 6.x for node 20'); process.exit(1)}" \
      && printf '<html><body><article><h1>t</h1><p>x</p></article></body></html>' > /tmp/rt.html \
      && node ExtractArticle.js -i /tmp/rt.html >/dev/null \
      && rm /tmp/rt.html || exit 1; \
    done

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
