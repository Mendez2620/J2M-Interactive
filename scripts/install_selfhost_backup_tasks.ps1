param(
    [string]$Repository = (Resolve-Path "$PSScriptRoot\.."),
    [string]$KeyFile = "$env:LOCALAPPDATA\J2M\backup-key.dpapi",
    [string]$CloudRemote = "",
    [switch]$Enable
)

$ErrorActionPreference = "Stop"
$taskPrefix = "J2M SelfHost"
$hourlyArgs = "-NoProfile -NonInteractive -File `"$Repository\scripts\run_selfhost_backup.ps1`" -Repository `"$Repository`" -KeyFile `"$KeyFile`""
$cloudArgs = "-NoProfile -NonInteractive -File `"$Repository\scripts\run_selfhost_cloud_copy.ps1`" -Repository `"$Repository`" -Remote `"$CloudRemote`""
$restoreArgs = "-NoProfile -NonInteractive -File `"$Repository\scripts\run_selfhost_restore_test.ps1`" -Repository `"$Repository`" -KeyFile `"$KeyFile`""

if (-not $Enable) {
    Write-Host "PREPARACION: no se registraron tareas. Use -Enable solo tras autorizar clave, proveedor y destino cloud."
    Write-Host "Tareas previstas: respaldo horario, copia cada cuatro horas y restauracion semanal aislada."
    exit 0
}
if (-not (Test-Path -LiteralPath $KeyFile)) { throw "No existe la clave DPAPI local." }
if ([string]::IsNullOrWhiteSpace($CloudRemote)) { throw "CloudRemote es obligatorio para activar copias externas." }
if (-not (Get-Command rclone -ErrorAction SilentlyContinue)) { throw "rclone no está instalado." }

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$hourly = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Hours 1)
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $hourlyArgs
Register-ScheduledTask -TaskName "$taskPrefix Database Hourly" -Action $action -Trigger $hourly -Principal $principal -Settings $settings -Force | Out-Null

$cloud = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(20) -RepetitionInterval (New-TimeSpan -Hours 4)
$cloudAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $cloudArgs
Register-ScheduledTask -TaskName "$taskPrefix Cloud Four Hour" -Action $cloudAction -Trigger $cloud -Principal $principal -Settings $settings -Force | Out-Null

$weekly = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "03:00"
$restoreAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $restoreArgs
Register-ScheduledTask -TaskName "$taskPrefix Restore Weekly" -Action $restoreAction -Trigger $weekly -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Tareas horaria, cloud y restauracion aislada semanal registradas."
