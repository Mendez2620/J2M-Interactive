param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string]$ApprovedSha,
    [string]$EnvFile = ".env.selfhost",
    [string]$LocalHttpsOrigin = "https://localhost:8443",
    [switch]$WithTunnel
)

$ErrorActionPreference = "Stop"

function Test-J2MLocalHttps([string]$Origin, [string]$ExpectedRevision) {
    $uri = $null
    if (-not [Uri]::TryCreate($Origin, [UriKind]::Absolute, [ref]$uri) -or
        $uri.Scheme -ne "https" -or $uri.Host -ne "localhost" -or
        $uri.AbsolutePath -ne "/" -or -not [string]::IsNullOrEmpty($uri.Query) -or
        -not [string]::IsNullOrEmpty($uri.Fragment)) {
        throw "La comprobación posterior exige el origen HTTPS local exacto."
    }
    foreach ($path in @("/api/health", "/api/ready")) {
        $response = Invoke-WebRequest -UseBasicParsing -Uri ($Origin.TrimEnd("/") + $path) -TimeoutSec 20
        if ($response.StatusCode -ne 200 -or [string]$response.Headers["X-J2M-Revision"] -cne $ExpectedRevision) {
            throw "HTTPS no sirve la revisión exacta aprobada."
        }
    }
}

function Confirm-J2MLocalProxy([string]$Origin, [string]$ExpectedImage) {
    if ($ExpectedImage -cnotmatch '^j2m-interactive:([0-9a-f]{40})$') {
        throw "La imagen esperada no tiene un SHA aprobado."
    }
    $expectedRevision = $Matches[1]
    & python "$PSScriptRoot\selfhost_proxy_recovery.py" `
        --origin $Origin `
        --expected-image $ExpectedImage `
        --expected-revision $expectedRevision `
        --attempts 8 `
        --retry-seconds 1.5 `
        --request-timeout 5
    if ($LASTEXITCODE -ne 0) {
        throw "No se pudo confirmar la resolución del proxy HTTPS local."
    }
    # Require a second, independent TLS check after any no-op or graceful reload.
    Test-J2MLocalHttps $Origin $expectedRevision
}

if (git status --porcelain) { throw "El árbol de trabajo debe estar limpio." }
git fetch origin main
if ($LASTEXITCODE -ne 0) { throw "No se pudo actualizar origin/main." }
$mainSha = (git rev-parse origin/main).Trim()
$headSha = (git rev-parse HEAD).Trim()
if ($ApprovedSha -ne $mainSha -or $headSha -ne $ApprovedSha) {
    throw "Solo se permite desplegar el SHA exacto actualmente aprobado en origin/main y presente en HEAD."
}
if (-not (Test-Path -LiteralPath $EnvFile)) { throw "Falta el archivo privado de entorno." }

$previousPins = @(Get-Content -LiteralPath $EnvFile | Where-Object { $_ -match '^J2M_IMAGE=j2m-interactive:([0-9a-f]{40})$' })
if ($previousPins.Count -ne 1) { throw "El entorno debe contener un único pin anterior recuperable." }
$previousSha = $previousPins[0].Substring("J2M_IMAGE=j2m-interactive:".Length)
$previousImage = "j2m-interactive:$previousSha"
docker image inspect $previousImage *> $null
if ($LASTEXITCODE -ne 0) { throw "La imagen anterior no existe localmente; no se puede garantizar rollback." }
if ($ApprovedSha -eq $previousSha) {
    throw "El SHA solicitado ya está fijado; se rechaza reconstruir la única imagen disponible para rollback."
}

& "$PSScriptRoot\invoke_selfhost_compose.ps1" -EnvFile $EnvFile config --quiet
if ($LASTEXITCODE -ne 0) { throw "La configuración persistente anterior no es recuperable." }

$imageTag = "j2m-interactive:$ApprovedSha"
docker build --pull --tag $imageTag .
if ($LASTEXITCODE -ne 0) { throw "Falló la construcción de la imagen." }

$services = @("web", "google-wallet-worker")
$profiles = @()
if ($WithTunnel) { $profiles = @("--profile", "tunnel"); $services += "cloudflared" }
try {
    & "$PSScriptRoot\set_selfhost_image_pin.ps1" -ApprovedSha $ApprovedSha -EnvFile $EnvFile
    & "$PSScriptRoot\invoke_selfhost_compose.ps1" -EnvFile $EnvFile @profiles up -d --no-build --wait --wait-timeout 120 @services
    Confirm-J2MLocalProxy $LocalHttpsOrigin $imageTag
} catch {
    & "$PSScriptRoot\set_selfhost_image_pin.ps1" -ApprovedSha $previousSha -EnvFile $EnvFile
    try {
        & "$PSScriptRoot\invoke_selfhost_compose.ps1" -EnvFile $EnvFile up -d --no-build --wait --wait-timeout 120 web google-wallet-worker
        Confirm-J2MLocalProxy $LocalHttpsOrigin $previousImage
    } catch {
        throw "Falló el despliegue y no fue posible confirmar por HTTPS la recuperación de la imagen anterior."
    }
    throw "Falló el despliegue; la imagen y el pin anteriores fueron restaurados."
}
Write-Host "Versión aprobada iniciada. Este script no ejecuta migraciones."
