# --- Stage 1: build the React frontend ---
FROM node:20-slim AS frontend-build
WORKDIR /frontend

# Same optional corporate CA pattern as the final image, since npm also
# makes HTTPS calls (to the npm registry) that corporate SSL inspection
# can intercept.
COPY corporate-ca.cr[t] /usr/local/share/ca-certificates/
RUN update-ca-certificates || true
ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

COPY frontend/package.json frontend/package-lock.jso[n] ./
RUN npm install
COPY frontend/ .
RUN npm run build

# --- Stage 2: the Flask app, serving the built frontend ---
FROM python:3.11-slim

WORKDIR /app

# Corporate SSL-inspection fix (only needed on networks that intercept
# HTTPS, e.g. corporate proxies). The bracket-glob pattern below matches
# corporate-ca.crt if present but does NOT fail the build if it's absent —
# so this same Dockerfile works both on locked-down corporate networks and
# on a plain home/open network with no extra setup.
COPY corporate-ca.cr[t] /usr/local/share/ca-certificates/
RUN update-ca-certificates

# jira_rest.py points `requests` at the merged system bundle on Linux
# instead of using truststore's global SSL monkey-patch (which caused a
# recursion bug when combined with the MCP client's own SSL usage in the
# same process). This is a no-op if no corporate cert was added above —
# it just points at the standard bundle.
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The built React app replaces the old static/dashboard.html — Flask's
# /dashboard route (see app.py) now serves this instead.
COPY --from=frontend-build /frontend/dist ./static/react

# config/ (field_mappings.json, workflow_phases.json) is baked into the
# image by the COPY above, but can also be bind-mounted at container run
# time (-v ./config:/app/config) to edit and swap Jira-instance config
# without rebuilding the image — see README "Switching to a different
# Jira instance".

# compliance.db lives here so it can be mounted as a volume and survive
# container restarts/rebuilds — see docker-compose.yml
VOLUME ["/app/data"]
ENV COMPLIANCE_DB_PATH=/app/data/compliance.db

EXPOSE 5000

CMD ["python", "app.py"]
