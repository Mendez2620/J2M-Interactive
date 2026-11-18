param(
    [string]$EnvFile = ".env.selfhost",
    [string]$CloudflareEnvFile = ".env.cloudflared",
    [switch]$RequireTunnel,
    [switch]$SkipExternalConnectivity,
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

$envPath = Resolve-Path -LiteralPath $EnvFile -ErrorAction SilentlyContinue
Write-Check "private-env" ($null -ne $envPath) "archivo privado fuera de Git disponible"
if ($envPath) {
    $pairs = @{}
    foreach ($line in Get-Content -LiteralPath $envPath) {
        if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { $pairs[$Matches[1]] = $Matches[2] }
    }
    $required = @(
        "SECRET_KEY", "JWT_SECRET_KEY", "REGISTRATION_IDENTITY_HMAC_KEY",
        "REFERRAL_EVIDENCE_HMAC_KEYS", "DATABASE_URL", "DATABASE_SSL_ROOT_CERT", "PUBLIC_BASE_URL",
        "CORS_ORIGINS", "SESSION_COOKIE_SECURE", "APPLE_WALLET_MODE"
    )
    $missing = @($required | Where-Object { -not $pairs.ContainsKey($_) -or [string]::IsNullOrWhiteSpace($pairs[$_]) -or $pairs[$_] -match '^REQUIRED' })
    Write-Check "required-env" ($missing.Count -eq 0) ($(if ($missing) { "faltan nombres: " + ($missing -join ", ") } else { "nombres requeridos presentes" }))
    Write-Check "apple-disabled" ($pairs["APPLE_WALLET_MODE"] -eq "disabled") "Apple Wallet debe permanecer disabled"
    Write-Check "google-disabled" ($pairs["GOOGLE_WALLET_MODE"] -eq "disabled") "Google Wallet debe permanecer disabled"
    $databaseRootCert = if ($pairs.ContainsKey("DATABASE_SSL_ROOT_CERT")) { $pairs["DATABASE_SSL_ROOT_CERT"] } else { "" }
    Write-Check "database-root-ca" (-not [string]::IsNullOrWhiteSpace($databaseRootCert) -and (Test-Path -LiteralPath $databaseRootCert -PathType Leaf)) "CA PostgreSQL local disponible para verify-full"
    Write-Check "app-secret-separation" (-not $pairs.ContainsKey("TUNNEL_TOKEN") -and -not $pairs.ContainsKey("CLOUDFLARE_TUNNEL_TOKEN")) "el token del túnel no debe estar en el entorno de la aplicación"

    $configuredPorts = [System.Collections.Generic.List[int]]::new()
    $localPortText = if ($pairs.ContainsKey("J2M_LOCAL_PORT")) { $pairs["J2M_LOCAL_PORT"] } else { "8000" }
    $localPort = 0
    if ([int]::TryParse($localPortText, [ref]$localPort) -and $localPort -ge 1 -and $localPort -le 65535) {
        $configuredPorts.Add($localPort)
    } else {
        Write-Check "web-port-config" $false "J2M_LOCAL_PORT debe ser un puerto válido"
    }
    if ($RequireTunnel) {
        $metricsPortText = if ($pairs.ContainsKey("CLOUDFLARED_METRICS_PORT")) { $pairs["CLOUDFLARED_METRICS_PORT"] } else { "2000" }
        $metricsPort = 0
        if ([int]::TryParse($metricsPortText, [ref]$metricsPort) -and $metricsPort -ge 1 -and $metricsPort -le 65535) {
            $configuredPorts.Add($metricsPort)
        } else {
            Write-Check "tunnel-port-config" $false "CLOUDFLARED_METRICS_PORT debe ser un puerto válido"
        }
    }
    $busy = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in $configuredPorts })
    Write-Check "ports" ($busy.Count -eq 0) ($(if ($busy) { "ocupados: " + (($busy.LocalPort | Sort-Object -Unique) -join ", ") } else { "puertos configurados libres: " + (($configuredPorts | Sort-Object -Unique) -join ", ") } ))
}

if ($RequireTunnel) {
    $tunnelPath = Resolve-Path -LiteralPath $CloudflareEnvFile -ErrorAction SilentlyContinue
    Write-Check "tunnel-private-env" ($null -ne $tunnelPath) "archivo privado exclusivo de cloudflared disponible"
    if ($tunnelPath) {
        $tunnelPairs = @{}
        foreach ($line in Get-Content -LiteralPath $tunnelPath) {
            if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { $tunnelPairs[$Matches[1]] = $Matches[2] }
        }
        $appSecretNames = @("DATABASE_URL", "SECRET_KEY", "JWT_SECRET_KEY", "GOOGLE_SERVICE_ACCOUNT_JSON")
        $hasAppSecrets = @($appSecretNames | Where-Object { $tunnelPairs.ContainsKey($_) }).Count -gt 0
        $validToken = $tunnelPairs.ContainsKey("TUNNEL_TOKEN") -and $tunnelPairs["TUNNEL_TOKEN"] -notmatch '^\s*(REQUIRED)?\s*$'
        Write-Check "tunnel-token" $validToken "TUNNEL_TOKEN debe existir solo en el archivo del túnel"
        Write-Check "tunnel-secret-separation" (-not $hasAppSecrets) "cloudflared no debe recibir secretos de aplicación"
    }
}

if (-not $SkipExternalConnectivity) {
    foreach ($target in @("github.com", "supabase.com", "region1.v2.argotunnel.com")) {
        $port = if ($target -like "*.argotunnel.com") { 7844 } else { 443 }
        $reachable = Test-NetConnection $target -Port $port -InformationLevel Quiet -WarningAction SilentlyContinue
        Write-Check "network-$target" $reachable "TCP $port"
    }
} else {
    Write-Host "[OK] external-connectivity - omitida explícitamente para ensayo aislado"
}

if ($failures.Count) {
    Write-Error ("Preflight bloqueado: " + ($failures -join ", "))
    exit 1
}
Write-Host "Preflight local aprobado. No se realizaron migraciones ni despliegues."
