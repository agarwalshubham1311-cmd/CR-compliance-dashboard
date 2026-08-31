#!/usr/bin/env bash
# run.sh - single command to build and start the whole compliance dashboard stack.
# Usage:  ./run.sh
set -e

echo "== Compliance Dashboard: setup =="

if ! docker ps > /dev/null 2>&1; then
  echo "Docker doesn't seem to be running. Start Docker Desktop/daemon and try again."
  exit 1
fi

if [ ! -f jira.env ]; then
  echo "No jira.env file found. Create it with JIRA_URL, JIRA_USERNAME, JIRA_API_TOKEN first."
  exit 1
fi

if [ ! -f aws.env ]; then
  echo "No aws.env file found. Create it with AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION first."
  exit 1
fi

if docker compose version > /dev/null 2>&1; then
  echo "Using docker compose..."
  docker compose up -d --build
else
  echo "docker compose plugin not available - using manual build/run fallback..."

  docker network inspect compliance-net > /dev/null 2>&1 || docker network create compliance-net

  echo "Building jira-mcp..."
  docker build -t jira-mcp-custom -f Dockerfile.jira-mcp .
  docker rm -f jira-mcp > /dev/null 2>&1 || true
  docker run -d --name jira-mcp --network compliance-net -p 8000:8000 --env-file jira.env \
    jira-mcp-custom --transport sse --port 8000

  echo "Building compliance-dashboard..."
  docker build -t compliance-dashboard -f Dockerfile .
  docker rm -f compliance-dashboard > /dev/null 2>&1 || true
  docker run -d --name compliance-dashboard --network compliance-net -p 5000:5000 \
    --env-file jira.env --env-file aws.env \
    -e MCP_SERVER_URL=http://jira-mcp:8000/sse \
    -v compliance-data:/app/data \
    compliance-dashboard
fi

echo ""
echo "== Done =="
echo "Dashboard: http://localhost:5000/dashboard"
echo "Check status any time with: docker ps"
