param(
    [string]$EnvFile = ".env.selfhost",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ComposeArguments
)

$ErrorActionPreference = "Stop"
$projectName = "j2m-cutover-ready"
$envPath = Resolve-Path -LiteralPath $EnvFile -ErrorAction Stop
$pairs = @{}
foreach ($line in Get-Content -LiteralPath $envPath) {
    if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { $pairs[$Matches[1]] = $Matches[2] }
}
if (-not $pairs.ContainsKey("J2M_IMAGE") -or $pairs["J2M_IMAGE"] -notmatch '^j2m-interactive:[0-9a-f]{40}$') {
    throw "J2M_IMAGE no contiene una etiqueta aprobada exacta."
}
if (-not $ComposeArguments -or $ComposeArguments.Count -eq 0) {
    throw "Faltan argumentos de Docker Compose."
}

$workerIds = @(& docker ps -q --filter "label=com.docker.compose.service=google-wallet-worker")
if ($LASTEXITCODE -ne 0) { throw "No se pudo comprobar la exclusividad del worker." }
foreach ($workerId in $workerIds) {
    $labelsJson = (& docker inspect --format '{{json .Config.Labels}}' $workerId | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($labelsJson)) {
        throw "No se pudo verificar la propiedad del Google Wallet worker activo."
    }
    $labels = $labelsJson | ConvertFrom-Json
    $workerProject = $labels.'com.docker.compose.project'
    if ($workerProject -ne $projectName) {
        throw "Existe un Google Wallet worker activo fuera del proyecto autorizado."
    }
}

$previousImage = [Environment]::GetEnvironmentVariable("J2M_IMAGE", "Process")
$previousEnvFile = [Environment]::GetEnvironmentVariable("J2M_ENV_FILE", "Process")
try {
    # Process values have higher precedence than --env-file. Replace inherited
    # values with the verified private configuration for this child command.
    [Environment]::SetEnvironmentVariable("J2M_IMAGE", $pairs["J2M_IMAGE"], "Process")
    [Environment]::SetEnvironmentVariable("J2M_ENV_FILE", $envPath.Path, "Process")
    & docker compose --project-name $projectName --env-file $envPath.Path -f compose.selfhost.yml @ComposeArguments
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose devolvió un error." }
} finally {
    [Environment]::SetEnvironmentVariable("J2M_IMAGE", $previousImage, "Process")
    [Environment]::SetEnvironmentVariable("J2M_ENV_FILE", $previousEnvFile, "Process")
}
