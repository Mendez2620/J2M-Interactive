param(
    [Parameter(Mandatory = $true)][string]$Remote,
    [string]$Repository = (Resolve-Path "$PSScriptRoot\..")
)

$ErrorActionPreference = "Stop"
$failure = Join-Path $Repository "selfhost-backups\logs\cloud.failed"
try {
  if (-not (Get-Command rclone -ErrorAction SilentlyContinue)) { throw "rclone unavailable" }
  $source = Join-Path $Repository "selfhost-backups\hourly"
  if (-not (Test-Path -LiteralPath $source)) { throw "encrypted backups unavailable" }

  # copy, nunca sync: un borrado local no se propaga al proveedor.
  rclone copy $source "$Remote/four-hour" --include "*.j2mbackup" --include "*.sha256" --immutable --retries 3
  if ($LASTEXITCODE -ne 0) { throw "cloud copy failed" }
  rclone check $source "$Remote/four-hour" --one-way --include "*.j2mbackup" --include "*.sha256" --retries 3
  if ($LASTEXITCODE -ne 0) { throw "cloud integrity check failed" }
  $configuration = Join-Path $Repository "selfhost-backups\configuration"
  if (Test-Path -LiteralPath $configuration) {
    rclone copy $configuration "$Remote/configuration" --include "*.j2mbackup" --include "*.sha256" --immutable --retries 3
    if ($LASTEXITCODE -ne 0) { throw "configuration copy failed" }
    rclone check $configuration "$Remote/configuration" --one-way --include "*.j2mbackup" --include "*.sha256" --retries 3
    if ($LASTEXITCODE -ne 0) { throw "configuration integrity check failed" }
  }

  $latest = Get-ChildItem -LiteralPath $source -Filter "*.j2mbackup" | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
  if ($null -ne $latest -and (Get-Date).ToUniversalTime().Hour -lt 4) {
    rclone copyto $latest.FullName "$Remote/daily/$($latest.Name)" --immutable --retries 3
    if ($LASTEXITCODE -ne 0) { throw "daily copy failed" }
    rclone copyto ($latest.FullName + ".sha256") "$Remote/daily/$($latest.Name).sha256" --immutable --retries 3
    if ($LASTEXITCODE -ne 0) { throw "daily checksum copy failed" }
  }

  # La retencion remota es intencional y queda acotada a prefijos exclusivos.
  rclone delete "$Remote/four-hour" --min-age 7d --include "*.j2mbackup" --include "*.sha256" --retries 3
  if ($LASTEXITCODE -ne 0) { throw "four-hour retention failed" }
  rclone delete "$Remote/daily" --min-age 30d --include "*.j2mbackup" --include "*.sha256" --retries 3
  if ($LASTEXITCODE -ne 0) { throw "daily retention failed" }
  rclone delete "$Remote/configuration" --min-age 30d --include "*.j2mbackup" --include "*.sha256" --retries 3
  if ($LASTEXITCODE -ne 0) { throw "configuration retention failed" }
  Remove-Item -LiteralPath $failure -Force -ErrorAction SilentlyContinue
  exit 0
} catch {
  New-Item -ItemType Directory -Force -Path (Split-Path $failure) | Out-Null
  Set-Content -LiteralPath $failure -Value ((Get-Date).ToUniversalTime().ToString("o") + " cloud_copy_failed") -Encoding ascii
  exit 1
}
