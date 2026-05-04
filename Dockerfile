FROM python:3.11

# Security-Hardening (SECURITY-AUDIT C7):
# Dedizierter Non-Root-User, damit ein RCE im Container nicht direkt root erhält.
# Explizite UID/GID 1000, damit docker-compose tmpfs-Mounts mit uid=1000/gid=1000
# (siehe docker-compose.yml read_only-Hardening) deterministisch matchen.
RUN groupadd -g 1000 mirofish \
  && useradd -u 1000 -g mirofish -m -d /home/mirofish -s /bin/bash mirofish

# Node.js (>=18) und benoetigte Tools installieren
RUN apt-get update \
  && apt-get install -y --no-install-recommends nodejs npm \
  && rm -rf /var/lib/apt/lists/*

# uv aus offiziellem Astral-Image kopieren
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /uvx /bin/

WORKDIR /app
RUN chown -R mirofish:mirofish /app

# Ab hier laeuft alles unprivilegiert
USER mirofish

# Dependency-Manifeste fuer besseres Layer-Caching
COPY --chown=mirofish:mirofish package.json package-lock.json ./
COPY --chown=mirofish:mirofish frontend/package.json frontend/package-lock.json ./frontend/
COPY --chown=mirofish:mirofish backend/pyproject.toml backend/uv.lock ./backend/

# Abhaengigkeiten (Node + Python) installieren
RUN npm ci \
  && npm ci --prefix frontend \
  && cd backend && uv sync --frozen

# Restlichen Quellcode kopieren
COPY --chown=mirofish:mirofish . .

EXPOSE 3000 5001

# Frontend + Backend im Dev-Modus starten
CMD ["npm", "run", "dev"]
