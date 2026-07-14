$ErrorActionPreference = "Stop"
$Repo = "8w6s/xtalk"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Step([string]$Number, [string]$Title) {
    Write-Host "`n[$Number/4] $Title" -ForegroundColor Cyan
}
function Write-Ok([string]$Message) {
    Write-Host "  OK  " -NoNewline -ForegroundColor Green
    Write-Host $Message
}

Write-Host "+------------------------------------------------------------+" -ForegroundColor Cyan
Write-Host "| xtalk setup                                                |" -ForegroundColor White
Write-Host "| Cross-agent rooms for Claude, Codex and Antigravity        |" -ForegroundColor DarkGray
Write-Host "+------------------------------------------------------------+" -ForegroundColor Cyan
Write-Host "Project: " -NoNewline -ForegroundColor Magenta
Write-Host $Root
Write-Host "Source:  " -NoNewline -ForegroundColor Magenta
Write-Host "https://github.com/$Repo" -ForegroundColor DarkGray

Write-Step "1" "Environment checks"
$Python = Get-Command py -ErrorAction SilentlyContinue
if (-not $Python) { $Python = Get-Command python -ErrorAction SilentlyContinue }
if (-not $Python) { throw "Python 3.10+ is required." }
$Npx = Get-Command npx -ErrorAction SilentlyContinue
if (-not $Npx) { throw "Node.js/npx is required for agent skill installation." }
Write-Ok "Python: $(& $Python.Source --version)"
Write-Ok "Skills CLI runner: $($Npx.Source)"

Write-Step "2" "Install xtalk MCP server"
& $Python.Source "$Root/install.py" --mcp-only --quiet-pip @args
if ($LASTEXITCODE -ne 0) { throw "MCP installation failed." }
Write-Ok "Stable runtime, client config and doctor completed."

Write-Step "3" "Install the xtalk agent skill"
& $Python.Source "$Root/install.py" --skill-only @args
if ($LASTEXITCODE -ne 0) { throw "Agent skill installation failed." }
Write-Ok "Skill installed from github.com/$Repo."

Write-Step "4" "Finish"
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "Skill scope: Claude Code, Codex, Cursor, Antigravity CLI" -ForegroundColor Magenta
Write-Host "Restart each configured agent, then ask it to register with xtalk." -ForegroundColor DarkGray
