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

function Write-J2MTaskRuntimeEvent {
    param(
        [Parameter(Mandatory = $true)][string]$LogPath,
        [Parameter(Mandatory = $true)][string]$Event,
        [Parameter(Mandatory = $true)][ValidateSet("retry", "ok", "error")][string]$Status,
        [int]$Attempt = 0
    )

    $parent = Split-Path -Parent $LogPath
    if (-not [string]::IsNullOrWhiteSpace($parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    $entry = [ordered]@{
        timestamp = (Get-Date).ToUniversalTime().ToString("o")
        event = $Event
        status = $Status
        attempt = $Attempt
    } | ConvertTo-Json -Compress
    Add-Content -LiteralPath $LogPath -Value $entry -Encoding utf8
}

function Test-J2MDockerEngine {
    try {
        & docker info --format '{{.ServerVersion}}' *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Wait-J2MDockerReady {
    param(
        [Parameter(Mandatory = $true)][string]$LogPath,
        [double]$MaxWaitSeconds = 300,
        [double]$RetryIntervalSeconds = 30
    )

    if ($MaxWaitSeconds -le 0 -or $RetryIntervalSeconds -le 0) {
        throw "Los plazos de espera de Docker deben ser positivos."
    }

    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    $attempt = 0
    while ($true) {
        if ($attempt -gt 0 -and $stopwatch.Elapsed.TotalSeconds -ge $MaxWaitSeconds) {
            Write-J2MTaskRuntimeEvent -LogPath $LogPath -Event "docker_timeout" -Status "error" -Attempt $attempt
            throw "Docker no estuvo disponible dentro del plazo permitido."
        }
        $attempt += 1
        $previousExitCode = $global:LASTEXITCODE
        $ready = Test-J2MDockerEngine
        $global:LASTEXITCODE = $previousExitCode
        if ($ready) {
            if ($attempt -gt 1) {
                Write-J2MTaskRuntimeEvent -LogPath $LogPath -Event "docker_recovered" -Status "ok" -Attempt $attempt
            }
            return
        }

        Write-J2MTaskRuntimeEvent -LogPath $LogPath -Event "docker_unavailable" -Status "retry" -Attempt $attempt
        $remainingMilliseconds = [Math]::Max(1, [int](($MaxWaitSeconds - $stopwatch.Elapsed.TotalSeconds) * 1000))
        $intervalMilliseconds = [Math]::Max(1, [int]($RetryIntervalSeconds * 1000))
        Start-Sleep -Milliseconds ([Math]::Min($remainingMilliseconds, $intervalMilliseconds))
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
