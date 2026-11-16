param(
    [string]$EnvFile = ".env.selfhost",
    [int]$MinimumFreeGB = 20
)

$ErrorActionPreference = "Stop"
$failures = [System.Collections.Generic.List[string]]::new()

function Write-Check([string]$Name, [bool]$Ok, [string]$Detail) {
    $status = if ($Ok) { "OK" } else { "ERROR" }
    Write-Host "[$status] $Name - $Detail"
    if (-not $Ok) { $failures.Add($Name) }
}

$drive = Get-PSDrive -Name C
$freeGB = [math]::Round($drive.Free / 1GB, 2)
Write-Check "disk" ($freeGB -ge $MinimumFreeGB) "$freeGB GB libres; mínimo $MinimumFreeGB GB"

$wslText = (& wsl --status 2>&1 | Out-String)
Write-Check "wsl2" ($LASTEXITCODE -eq 0 -and $wslText -match "2") "WSL debe responder y usar versión 2"

$docker = Get-Command docker -ErrorAction SilentlyContinue
Write-Check "docker-cli" ($null -ne $docker) "Docker CLI instalado"
if ($docker) {
    & docker info *> $null
    Write-Check "docker-engine" ($LASTEXITCODE -eq 0) "Docker Engine accesible"
    & docker compose version *> $null
    Write-Check "docker-compose" ($LASTEXITCODE -eq 0) "Docker Compose v2 accesible"
}

$ports = @(80, 443, 2000, 5432, 8000, 8080)
$busy = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in $ports })
Write-Check "ports" ($busy.Count -eq 0) ($(if ($busy) { "ocupados: " + (($busy.LocalPort | Sort-Object -Unique) -join ", ") } else { "sin conflictos conocidos" }))

$envPath = Resolve-Path -LiteralPath $EnvFile -ErrorAction SilentlyContinue
Write-Check "private-env" ($null -ne $envPath) "archivo privado fuera de Git disponible"
if ($envPath) {
    $pairs = @{}
    foreach ($line in Get-Content -LiteralPath $envPath) {
        if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { $pairs[$Matches[1]] = $Matches[2] }
    }
    $required = @(
        "SECRET_KEY", "JWT_SECRET_KEY", "REGISTRATION_IDENTITY_HMAC_KEY",
        "REFERRAL_EVIDENCE_HMAC_KEYS", "DATABASE_URL", "PUBLIC_BASE_URL",
        "CORS_ORIGINS", "SESSION_COOKIE_SECURE", "APPLE_WALLET_MODE"
    )
    $missing = @($required | Where-Object { -not $pairs.ContainsKey($_) -or [string]::IsNullOrWhiteSpace($pairs[$_]) -or $pairs[$_] -match '^REQUIRED' })
    Write-Check "required-env" ($missing.Count -eq 0) ($(if ($missing) { "faltan nombres: " + ($missing -join ", ") } else { "nombres requeridos presentes" }))
    Write-Check "apple-disabled" ($pairs["APPLE_WALLET_MODE"] -eq "disabled") "Apple Wallet debe permanecer disabled"
}

foreach ($target in @("github.com", "supabase.com", "region1.v2.argotunnel.com")) {
    $port = if ($target -like "*.argotunnel.com") { 7844 } else { 443 }
    $reachable = Test-NetConnection $target -Port $port -InformationLevel Quiet -WarningAction SilentlyContinue
    Write-Check "network-$target" $reachable "TCP $port"
}

if ($failures.Count) {
    Write-Error ("Preflight bloqueado: " + ($failures -join ", "))
    exit 1
}
Write-Host "Preflight local aprobado. No se realizaron migraciones ni despliegues."
