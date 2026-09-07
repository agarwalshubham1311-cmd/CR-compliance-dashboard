# run.ps1 - single command to build and start the whole compliance dashboard stack.
# Usage:  .\run.ps1
#
# Tries "docker compose" first. If that's not available (compose plugin
# blocked by some corporate installs), falls back to plain "docker build"/
# "docker run" commands, with extra-defensive network handling since
# containers repeatedly ending up disconnected from compliance-net has
# been the single most common failure in this deployment.
#
# AI runs on AWS Bedrock (see aws.env) - no local model container needed.

function Invoke-Checked {
    param([string]$Description, [scriptblock]$Command)
    Write-Host $Description
    & $Command
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAILED: $Description (exit code $LASTEXITCODE)" -ForegroundColor Red
        Write-Host "Stopping here so this doesn't fail silently further down." -ForegroundColor Red
        exit 1
    }
}

function Test-DockerRunning {
    # try/catch alone does NOT work here - PowerShell doesn't throw
    # exceptions for a failed *native* command (only cmdlets), so a
    # failing "docker ps" would silently fall through to the try block's
    # own success path. $LASTEXITCODE is the only reliable check.
    docker ps *> $null
    return $LASTEXITCODE -eq 0
}

function Test-ComposeAvailable {
    docker compose version *> $null
    return $LASTEXITCODE -eq 0
}

function Test-ContainerExists {
    param([string]$Name)
    $result = docker ps -a --filter "name=^$Name`$" --format "{{.Names}}" 2>$null
    return $result -eq $Name
}

function Ensure-NetworkConnected {
    # Explicitly (re)connects a container to compliance-net, ignoring the
    # "already connected" error - this is the actual fix for the
    # recurring problem, since --network on docker run alone has not
    # reliably kept containers attached across restarts/rebuilds in this
    # setup.
    param([string]$ContainerName)
    docker network connect compliance-net $ContainerName 2>$null
    # Exit code is ignored on purpose: failure here almost always just
    # means "already connected", which is fine, not an error.
}

function Test-NetworkMembership {
    # Returns $true if the container actually shows up in compliance-net's
    # member list right now - the real verification, not just "did the
    # connect command not error".
    param([string]$ContainerName)
    $inspect = docker network inspect compliance-net --format '{{json .Containers}}' 2>$null
    if (-not $inspect) { return $false }
    return $inspect -match [regex]::Escape($ContainerName)
}

Write-Host "== Compliance Dashboard: setup ==" -ForegroundColor Cyan

if (-not (Test-DockerRunning)) {
    Write-Host "Docker doesn't seem to be running. Start Docker Desktop and try again." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path "jira.env")) {
    Write-Host "No jira.env file found. Create it with JIRA_URL, JIRA_USERNAME, JIRA_API_TOKEN first." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path "aws.env")) {
    Write-Host "No aws.env file found. Create it with AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION first." -ForegroundColor Red
    exit 1
}

if (Test-ComposeAvailable) {
    Write-Host "Using docker compose..." -ForegroundColor Green
    docker compose up -d --build
    if ($LASTEXITCODE -ne 0) {
        Write-Host "docker compose up failed (exit code $LASTEXITCODE) - see output above." -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "docker compose plugin not available - using manual build/run fallback..." -ForegroundColor Yellow

    docker network inspect compliance-net *> $null
    if ($LASTEXITCODE -ne 0) {
        Invoke-Checked "Creating compliance-net network..." { docker network create compliance-net }
    }

    # --- jira-mcp ---
    Invoke-Checked "Building jira-mcp..." { docker build -t jira-mcp-custom -f Dockerfile.jira-mcp . }
    if (Test-ContainerExists "jira-mcp") {
        Write-Host "jira-mcp container already exists, starting it..."
        docker start jira-mcp | Out-Null
    } else {
        Invoke-Checked "Starting jira-mcp (fresh container)..." {
            docker run -d --name jira-mcp --network compliance-net -p 8000:8000 --env-file jira.env `
                jira-mcp-custom --transport sse --port 8000
        }
    }
    Ensure-NetworkConnected "jira-mcp"

    # --- compliance-dashboard ---
    Invoke-Checked "Building compliance-dashboard..." { docker build -t compliance-dashboard -f Dockerfile . }
    if (Test-ContainerExists "compliance-dashboard") {
        docker rm -f compliance-dashboard | Out-Null  # always recreate this one - it's the app, not external state
    }
    Invoke-Checked "Starting compliance-dashboard..." {
        docker run -d --name compliance-dashboard --network compliance-net -p 5000:5000 `
            --env-file jira.env --env-file aws.env `
            -e MCP_SERVER_URL=http://jira-mcp:8000/sse `
            -v compliance-data:/app/data `
            compliance-dashboard
    }
    Ensure-NetworkConnected "compliance-dashboard"

    # --- Verify everyone actually made it onto the network ---
    Write-Host ""
    Write-Host "Verifying network membership..." -ForegroundColor Cyan
    $allGood = $true
    foreach ($name in @("jira-mcp", "compliance-dashboard")) {
        if (Test-NetworkMembership $name) {
            Write-Host "  OK: $name is on compliance-net" -ForegroundColor Green
        } else {
            Write-Host "  MISSING: $name is NOT on compliance-net" -ForegroundColor Red
            $allGood = $false
        }
    }
    if (-not $allGood) {
        Write-Host ""
        Write-Host "One or more containers failed to join the network. Try:" -ForegroundColor Red
        Write-Host "  docker network rm compliance-net" -ForegroundColor Yellow
        Write-Host "  then run this script again." -ForegroundColor Yellow
        exit 1
    }
}

Write-Host ""
Write-Host "== Done ==" -ForegroundColor Cyan
Write-Host "Dashboard: http://localhost:5000/dashboard" -ForegroundColor Green
Write-Host "Check status any time with: docker ps"
Write-Host "Check network membership any time with: docker network inspect compliance-net"
