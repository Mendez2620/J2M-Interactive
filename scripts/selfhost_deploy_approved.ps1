param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string]$ApprovedSha,
    [string]$EnvFile = ".env.selfhost",
    [switch]$WithTunnel
)

$ErrorActionPreference = "Stop"
if (git status --porcelain) { throw "El árbol de trabajo debe estar limpio." }
git fetch origin main
if ($LASTEXITCODE -ne 0) { throw "No se pudo actualizar origin/main." }
$mainSha = (git rev-parse origin/main).Trim()
$headSha = (git rev-parse HEAD).Trim()
if ($ApprovedSha -ne $mainSha -or $headSha -ne $ApprovedSha) {
    throw "Solo se permite desplegar el SHA exacto actualmente aprobado en origin/main y presente en HEAD."
}
if (-not (Test-Path -LiteralPath $EnvFile)) { throw "Falta el archivo privado de entorno." }

$env:J2M_ENV_FILE = $EnvFile
$env:J2M_IMAGE = "j2m-interactive:$ApprovedSha"
docker build --pull --tag $env:J2M_IMAGE .
if ($LASTEXITCODE -ne 0) { throw "Falló la construcción de la imagen." }

$services = @("web", "google-wallet-worker")
$profiles = @()
if ($WithTunnel) { $profiles = @("--profile", "tunnel"); $services += "cloudflared" }
docker compose --env-file $EnvFile -f compose.selfhost.yml @profiles up -d --no-build @services
if ($LASTEXITCODE -ne 0) { throw "Falló el arranque controlado." }
Write-Host "Versión aprobada iniciada. Este script no ejecuta migraciones."
