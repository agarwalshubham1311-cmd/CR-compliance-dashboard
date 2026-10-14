# Run from: D:\Homeoffice\Jira-Dashboard\CR-compliance-dashboard
$IssueKey = "ACC-34549"   # <-- replace with a real Jira issue key

Write-Host "== 1) Start services ==" -ForegroundColor Cyan
docker compose up -d --build jira-mcp flask-app
if ($LASTEXITCODE -ne 0) { throw "Failed to start docker compose services." }

Write-Host "`n== 2) Wait for jira-mcp SSE endpoint ==" -ForegroundColor Cyan
$maxAttempts = 20
$ready = $false
for ($i = 1; $i -le $maxAttempts; $i++) {
    try {
        $resp = Invoke-WebRequest -Uri "http://localhost:8000/sse" -Method Get -TimeoutSec 3
        if ($resp.StatusCode -ge 200) {
            $ready = $true
            Write-Host "MCP endpoint reachable (attempt $i)." -ForegroundColor Green
            break
        }
    } catch {
        Start-Sleep -Seconds 2
    }
}
if (-not $ready) {
    Write-Host "MCP endpoint not reachable." -ForegroundColor Red
    docker compose logs jira-mcp --tail 120
    exit 1
}

Write-Host "`n== 3) MCP tool discovery via flask-app ==" -ForegroundColor Cyan
try {
    $tools = Invoke-RestMethod -Uri "http://localhost:5000/api/tools" -Method Get -TimeoutSec 20
    Write-Host "Tool discovery OK. Tool count: $($tools.Count)" -ForegroundColor Green
} catch {
    Write-Host "Tool discovery failed." -ForegroundColor Red
    docker compose logs flask-app --tail 120
    docker compose logs jira-mcp --tail 120
    throw
}

Write-Host "`n== 4) Fetch one Jira ticket through MCP ==" -ForegroundColor Cyan
try {
    $issue = Invoke-RestMethod -Uri ("http://localhost:5000/api/issues/{0}" -f $IssueKey) -Method Get -TimeoutSec 30
    Write-Host "SUCCESS: Jira ticket fetch worked through jira-mcp for $IssueKey" -ForegroundColor Green
    $issue | ConvertTo-Json -Depth 6
} catch {
    Write-Host "FAILED: Could not fetch $IssueKey through jira-mcp." -ForegroundColor Red
    Write-Host "Check auth/permissions (403) vs key validity (404)." -ForegroundColor Yellow
    docker compose logs flask-app --tail 150
    docker compose logs jira-mcp --tail 150
    throw
}