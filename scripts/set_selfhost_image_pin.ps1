param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string]$ApprovedSha,
    [string]$EnvFile = ".env.selfhost"
)

$ErrorActionPreference = "Stop"

function Get-J2MFileAcl([string]$Path) {
    $fileInfo = Get-Item -LiteralPath $Path
    $extensionType = [Type]::GetType("System.IO.FileSystemAclExtensions, System.IO.FileSystem.AccessControl", $false)
    if ($null -ne $extensionType) { return $extensionType::GetAccessControl($fileInfo) }
    return [System.IO.File]::GetAccessControl($Path)
}

if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw "Falta el archivo privado de entorno."
}

git merge-base --is-ancestor $ApprovedSha origin/main
if ($LASTEXITCODE -ne 0) {
    throw "El SHA solicitado no pertenece al historial aprobado de origin/main."
}

$imageTag = "j2m-interactive:$ApprovedSha"
docker image inspect $imageTag *> $null
if ($LASTEXITCODE -ne 0) {
    throw "La imagen aprobada no existe localmente; constrúyela antes de fijarla."
}

$envPath = (Resolve-Path -LiteralPath $EnvFile).Path
$originalAcl = Get-J2MFileAcl $envPath
if (-not $originalAcl.AreAccessRulesProtected) {
    throw "El archivo privado debe tener una ACL protegida antes de modificarlo."
}
$lines = [System.IO.File]::ReadAllLines($envPath)
$updated = [System.Collections.Generic.List[string]]::new()
$found = $false
foreach ($line in $lines) {
    if ($line -match '^J2M_IMAGE=') {
        if (-not $found) { $updated.Add("J2M_IMAGE=$imageTag") }
        $found = $true
    } else {
        $updated.Add($line)
    }
}
if (-not $found) { $updated.Add("J2M_IMAGE=$imageTag") }

$temporary = "$envPath.$([guid]::NewGuid().ToString('N')).tmp"
try {
    # Create an empty placeholder, lock down its DACL, and only then write the
    # environment contents. Credentials never exist in an inherited-ACL temp.
    [System.IO.File]::WriteAllBytes($temporary, [byte[]]::new(0))
    & "$PSScriptRoot\protect_selfhost_secrets.ps1" -Path $temporary *> $null
    [System.IO.File]::WriteAllLines($temporary, $updated, [System.Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $envPath -Force
    & "$PSScriptRoot\protect_selfhost_secrets.ps1" -Path $envPath *> $null
} finally {
    Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
}

$pins = @(Get-Content -LiteralPath $envPath | Where-Object { $_ -match '^J2M_IMAGE=' })
if ($pins.Count -ne 1 -or $pins[0] -ne "J2M_IMAGE=$imageTag") {
    throw "No se pudo verificar el pin persistente de la imagen."
}
$resultAcl = Get-J2MFileAcl $envPath
if (-not $resultAcl.AreAccessRulesProtected) {
    throw "El archivo privado perdió la protección de su ACL."
}
Write-Host "Imagen aprobada fijada de forma persistente: $imageTag"
