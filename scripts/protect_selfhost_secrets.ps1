param(
    [Parameter(Mandatory = $true)]
    [string[]]$Path
)

$ErrorActionPreference = "Stop"
$currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value

foreach ($item in $Path) {
    $resolved = (Resolve-Path -LiteralPath $item -ErrorAction Stop).Path
    $isDirectory = (Get-Item -LiteralPath $resolved).PSIsContainer
    $currentGrant = if ($isDirectory) { "*$currentSid`:(OI)(CI)F" } else { "*$currentSid`:F" }
    $systemGrant = if ($isDirectory) { "*S-1-5-18:(OI)(CI)F" } else { "*S-1-5-18:F" }
    $adminGrant = if ($isDirectory) { "*S-1-5-32-544:(OI)(CI)F" } else { "*S-1-5-32-544:F" }

    # icacls avoids Set-Acl attempting to read/write SACL data without SeSecurityPrivilege.
    & icacls.exe $resolved /inheritance:r /grant:r $currentGrant $systemGrant $adminGrant *> $null
    if ($LASTEXITCODE -ne 0) { throw "No fue posible restringir los permisos del destino." }

    # PowerShell 7 exposes ACL access through FileSystemAclExtensions while
    # Windows PowerShell 5.1 exposes the legacy Directory/File static methods.
    # Select the available API so the protection script works in both hosts.
    $fileSystemInfo = Get-Item -LiteralPath $resolved
    $aclExtensionType = [Type]::GetType("System.IO.FileSystemAclExtensions, System.IO.FileSystem.AccessControl", $false)
    if ($null -ne $aclExtensionType) {
        $acl = $aclExtensionType::GetAccessControl($fileSystemInfo)
    } elseif ($isDirectory) {
        $acl = [System.IO.Directory]::GetAccessControl($resolved)
    } else {
        $acl = [System.IO.File]::GetAccessControl($resolved)
    }
    if (-not $acl.AreAccessRulesProtected) { throw "La herencia ACL continúa habilitada." }
    $allowed = @($currentSid, "S-1-5-18", "S-1-5-32-544")
    $unexpected = @($acl.Access | Where-Object {
        $_.AccessControlType -eq "Allow" -and $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -notin $allowed
    })
    if ($unexpected.Count -gt 0) { throw "La ACL contiene identidades no autorizadas." }
    Write-Host "Permisos restringidos: $resolved"
}
