FROM python:3.11-slim

WORKDIR /app

# Same corporate SSL-inspection fix used for jira-mcp and ollama —
# bake the corporate root CA into the image's trust store. The file
# must exist in the build context (same folder as this Dockerfile)
# before building. See README section "Docker build" for how to get it.
COPY corporate-ca.crt /usr/local/share/ca-certificates/corporate-ca.crt
RUN update-ca-certificates

# jira_rest.py points `requests` at this merged bundle on Linux instead of
# using truststore's global SSL monkey-patch, which caused a recursion bug
# when combined with the MCP client's own SSL usage in the same process.
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# compliance.db lives here so it can be mounted as a volume and survive
# container restarts/rebuilds — see docker-compose.yml
VOLUME ["/app/data"]
ENV COMPLIANCE_DB_PATH=/app/data/compliance.db

EXPOSE 5000

CMD ["python", "app.py"]
