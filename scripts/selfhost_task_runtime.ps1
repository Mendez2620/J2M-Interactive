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

function Stop-J2MProbeProcess {
    param(
        [Parameter(Mandatory = $true)]$Process,
        [Parameter(Mandatory = $true)][int]$TimeoutMilliseconds
    )

    try {
        $Process.Kill()
    } catch {
        return $false
    }
    if ($TimeoutMilliseconds -le 0) { return $false }
    try {
        # Always use the bounded overload. Cleanup must never extend the
        # probe's remaining budget or wait indefinitely during host startup.
        return $Process.WaitForExit($TimeoutMilliseconds)
    } catch {
        return $false
    }
}

function Invoke-J2MProcessWithTimeout {
    param(
        [Parameter(Mandatory = $true)][string]$FileName,
        [Parameter(Mandatory = $true)][string]$Arguments,
        [Parameter(Mandatory = $true)][double]$TimeoutSeconds
    )

    $process = [System.Diagnostics.Process]::new()
    try {
        $process.StartInfo = [System.Diagnostics.ProcessStartInfo]::new()
        $process.StartInfo.FileName = $FileName
        $process.StartInfo.Arguments = $Arguments
        $process.StartInfo.UseShellExecute = $false
        $process.StartInfo.CreateNoWindow = $true
        $process.StartInfo.RedirectStandardOutput = $true
        $process.StartInfo.RedirectStandardError = $true
        if (-not $process.Start()) { return "unavailable" }
        $outputTask = $process.StandardOutput.ReadToEndAsync()
        $errorTask = $process.StandardError.ReadToEndAsync()
        $totalMilliseconds = [Math]::Max(2, [int]($TimeoutSeconds * 1000))
        $cleanupReserveMilliseconds = [Math]::Min(2000, [Math]::Max(1, [int]($totalMilliseconds / 5)))
        $executionMilliseconds = [Math]::Max(1, $totalMilliseconds - $cleanupReserveMilliseconds)
        $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
        if (-not $process.WaitForExit($executionMilliseconds)) {
            # Kill only this disposable CLI probe. Never target Docker Desktop,
            # its backend, the engine, or any running container.
            $cleanupMilliseconds = [Math]::Max(1, $totalMilliseconds - [int]$stopwatch.ElapsedMilliseconds)
            Stop-J2MProbeProcess -Process $process -TimeoutMilliseconds $cleanupMilliseconds | Out-Null
            return "timeout"
        }
        if ($process.ExitCode -eq 0) { return "ready" }
        return "unavailable"
    } catch {
        return "unavailable"
    } finally {
        $process.Dispose()
    }
}

function Test-J2MDockerEngine {
    param([double]$TimeoutSeconds = 10)
    try {
        # Some Windows Docker Desktop installations expose both `docker.exe`
        # and an extensionless `docker` application. Always select exactly one
        # executable so PowerShell cannot coerce multiple paths into FileName.
        $command = Get-Command docker.exe -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -eq $command) {
            $command = Get-Command docker -CommandType Application -ErrorAction Stop |
                Select-Object -First 1
        }
        return Invoke-J2MProcessWithTimeout -FileName $command.Source -Arguments "info --format `"{{.ServerVersion}}`"" -TimeoutSeconds $TimeoutSeconds
    } catch {
        return "unavailable"
    }
}

function Wait-J2MDockerReady {
    param(
        [Parameter(Mandatory = $true)][string]$LogPath,
        [double]$MaxWaitSeconds = 300,
        [double]$RetryIntervalSeconds = 30,
        [double]$ProbeTimeoutSeconds = 10
    )

    if ($MaxWaitSeconds -le 0 -or $RetryIntervalSeconds -le 0 -or $ProbeTimeoutSeconds -le 0) {
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
        $attemptStartedSeconds = $stopwatch.Elapsed.TotalSeconds
        $remainingSeconds = [Math]::Max(0.001, $MaxWaitSeconds - $attemptStartedSeconds)
        $previousExitCode = $global:LASTEXITCODE
        $probeStatus = Test-J2MDockerEngine -TimeoutSeconds ([Math]::Min($ProbeTimeoutSeconds, $remainingSeconds))
        $global:LASTEXITCODE = $previousExitCode
        if ($probeStatus -eq "ready") {
            if ($attempt -gt 1) {
                Write-J2MTaskRuntimeEvent -LogPath $LogPath -Event "docker_recovered" -Status "ok" -Attempt $attempt
            }
            return
        }

        $event = if ($probeStatus -eq "timeout") { "docker_probe_timeout" } else { "docker_unavailable" }
        Write-J2MTaskRuntimeEvent -LogPath $LogPath -Event $event -Status "retry" -Attempt $attempt
        $remainingMilliseconds = [Math]::Max(1, [int](($MaxWaitSeconds - $stopwatch.Elapsed.TotalSeconds) * 1000))
        $nextAttemptSeconds = $attemptStartedSeconds + $RetryIntervalSeconds
        $intervalMilliseconds = [Math]::Max(1, [int](($nextAttemptSeconds - $stopwatch.Elapsed.TotalSeconds) * 1000))
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
