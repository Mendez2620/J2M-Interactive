function Send-J2MHealthcheck {
    param(
        [Parameter(Mandatory = $true)][string]$CheckName,
        [Parameter(Mandatory = $true)][ValidateSet("start", "success", "fail")][string]$State
    )

    # Monitoring is observational only. Preserve the protected operation's
    # process status even if Python cannot start or the endpoint is unavailable.
    $previousExitCode = $global:LASTEXITCODE
    try {
        & python -m scripts.healthchecks_monitor $CheckName $State *> $null
    } catch {
        # Deliberately ignored: Healthchecks must never decide backup success.
    } finally {
        $global:LASTEXITCODE = $previousExitCode
    }
}

function Enter-J2MSelfHostTaskGuard {
    param(
        [int]$TimeoutSeconds = 3600,
        [string]$Name = "Local\J2M-SelfHost-Backup-Operations"
    )

    $mutex = [System.Threading.Mutex]::new($false, $Name)
    $acquired = $false
    try {
        try {
            $acquired = $mutex.WaitOne([TimeSpan]::FromSeconds($TimeoutSeconds))
        } catch [System.Threading.AbandonedMutexException] {
            # The previous process died. Windows transferred ownership safely.
            $acquired = $true
        }
        if (-not $acquired) {
            throw "Otra tarea J2M no liberó el bloqueo dentro del plazo permitido."
        }
        return $mutex
    } catch {
        if (-not $acquired) { $mutex.Dispose() }
        throw
    }
}

function Exit-J2MSelfHostTaskGuard {
    param([System.Threading.Mutex]$Mutex)
    if ($null -eq $Mutex) { return }
    try {
        $Mutex.ReleaseMutex()
    } finally {
        $Mutex.Dispose()
    }
}
