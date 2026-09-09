param([ValidateSet('setup','repair','restore','demo','check')][string]$Mode = 'setup')
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$kaizoRuntime = $null
$kaizoExit = 1
$kaizoLog = $null
$kaizoStarted = Get-Date
$kaizoStage = 'create-log'
function Protect-LogText([string]$Text) {
    $Text = $Text -replace '(?i)\bsk-[A-Za-z0-9_-]+', '[KEY]'
    $Text = $Text -replace '(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+@', '$1[CREDENTIALS]@'
    $Text = $Text -replace '(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [TOKEN]'
    $Text = $Text -replace '\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[TOKEN]'
    $Text = $Text -replace '(?i)((?:access|refresh|id)_token|api[_-]?key)(["'']?\s*[:=]\s*["'']?)[^\s"'',;}]+', '$1$2[TOKEN]'
    return ($Text -replace '[\x00-\x1f\x7f]', ' ')
}
function Write-SetupLog([string]$Level, [string]$Message) {
    if ($kaizoLog) {
        $line = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $Level + ' BOOTSTRAP ' + (Protect-LogText $Message) + [Environment]::NewLine
        [IO.File]::AppendAllText($kaizoLog, $line, (New-Object Text.UTF8Encoding($false)))
    }
}
try {
    $kaizoName = 'setup-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + $PID + '.log'
    $kaizoLogDirs = @((Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'KAIZO-Setup\Logs'), (Join-Path $PSScriptRoot 'logs'))
    foreach ($kaizoDir in $kaizoLogDirs) {
        try {
            [IO.Directory]::CreateDirectory($kaizoDir) | Out-Null
            $kaizoCandidate = Join-Path $kaizoDir $kaizoName
            [IO.File]::AppendAllText($kaizoCandidate, '', (New-Object Text.UTF8Encoding($false)))
            $kaizoLog = $kaizoCandidate
            break
        } catch { }
    }
    if (-not $kaizoLog) { throw 'Cannot create a log. Extract the package to a folder writable by your current user.' }
    $env:KAIZO_SETUP_LOG = $kaizoLog
    Write-Host ('Log: ' + $kaizoLog) -ForegroundColor DarkGray
    Write-SetupLog 'INFO' ('START mode=' + $Mode + ' powershell=' + $PSVersionTable.PSVersion + ' os=' + [Environment]::OSVersion.VersionString + ' package=' + $PSScriptRoot)
    $kaizoStage = 'check-architecture'
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'Windows x64 or ARM64 is required.' }
    $kaizoArch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') { 'arm64' } else { 'x64' }
    Write-SetupLog 'INFO' ('ARCH ' + $kaizoArch)
    $kaizoStage = 'verify-runtime'
    $kaizoManifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'assets\manifest.json') -Raw | ConvertFrom-Json
    $kaizoPackage = $kaizoManifest.$kaizoArch.python
    $kaizoZip = Join-Path $PSScriptRoot ('assets\' + $kaizoPackage.file)
    if (-not (Test-Path -LiteralPath $kaizoZip)) {
        $kaizoStage = 'download-runtime'
        $kaizoCache = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'KAIZO-Setup\Downloads'
        [IO.Directory]::CreateDirectory($kaizoCache) | Out-Null
        $kaizoZip = Join-Path $kaizoCache $kaizoPackage.file
        $kaizoCached = (Test-Path -LiteralPath $kaizoZip) -and ((Get-FileHash -LiteralPath $kaizoZip -Algorithm SHA256).Hash.ToLowerInvariant() -eq $kaizoPackage.sha256)
        if (-not $kaizoCached) {
            $kaizoPartial = Join-Path $kaizoCache ([Guid]::NewGuid().ToString('N') + '.download')
            try {
                Write-Host 'First run: downloading the verified Python runtime...' -ForegroundColor Cyan
                Write-SetupLog 'INFO' ('DOWNLOAD_START file=' + $kaizoPackage.file)
                & "$env:SystemRoot\System32\curl.exe" -q --fail --location --silent --show-error --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 600 --output $kaizoPartial $kaizoPackage.url
                if ($LASTEXITCODE -ne 0) { throw ('Runtime download failed. curl exit code: ' + $LASTEXITCODE + '. Check network or proxy settings, then retry Setup.cmd.') }
                if ((Get-FileHash -LiteralPath $kaizoPartial -Algorithm SHA256).Hash.ToLowerInvariant() -ne $kaizoPackage.sha256) {
                    throw 'Downloaded runtime checksum mismatch; it was not executed.'
                }
                Move-Item -LiteralPath $kaizoPartial -Destination $kaizoZip -Force
                Write-SetupLog 'INFO' ('DOWNLOAD_DONE file=' + $kaizoPackage.file)
            } finally {
                if (Test-Path -LiteralPath $kaizoPartial) { Remove-Item -LiteralPath $kaizoPartial -Force }
            }
        }
    }
    $kaizoStage = 'verify-runtime'
    Write-Host 'KAIZO / Windows setup - verifying bundled runtime...' -ForegroundColor Cyan
    if ((Get-FileHash -LiteralPath $kaizoZip -Algorithm SHA256).Hash.ToLowerInvariant() -ne $kaizoPackage.sha256) {
        throw 'Runtime checksum mismatch. Please extract a fresh copy of this package.'
    }
    Write-SetupLog 'INFO' ('CHECKSUM_OK file=' + $kaizoZip)
    $kaizoStage = 'create-runtime-directory'
    $kaizoRuntime = Join-Path ([IO.Path]::GetTempPath()) ('kaizo-runtime-' + [Guid]::NewGuid().ToString('N'))
    Write-SetupLog 'INFO' ('RUNTIME_CREATE path=' + $kaizoRuntime)
    New-Item -ItemType Directory -Path $kaizoRuntime | Out-Null
    $kaizoStage = 'protect-runtime-directory'
    $kaizoSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & "$env:SystemRoot\System32\icacls.exe" $kaizoRuntime '/inheritance:r' '/grant:r' ('*' + $kaizoSid + ':(OI)(CI)(F)') '*S-1-5-18:(OI)(CI)(F)' '*S-1-5-32-544:(OI)(CI)(F)' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw ('Unable to protect the temporary runtime folder. icacls exit code: ' + $LASTEXITCODE) }
    $kaizoStage = 'extract-runtime'
    Write-SetupLog 'INFO' 'EXTRACT_START'
    Expand-Archive -LiteralPath $kaizoZip -DestinationPath $kaizoRuntime
    Write-SetupLog 'INFO' 'EXTRACT_DONE'
    $kaizoStage = 'run-setup'
    $kaizoScript = if ($Mode -eq 'check') { 'check_setup.py' } else { 'setup.py' }
    Write-SetupLog 'INFO' ('PYTHON_START script=' + $kaizoScript)
    if ($Mode -eq 'check') {
        & (Join-Path $kaizoRuntime 'python.exe') -X utf8 (Join-Path $PSScriptRoot $kaizoScript)
    } else {
        & (Join-Path $kaizoRuntime 'python.exe') -X utf8 (Join-Path $PSScriptRoot $kaizoScript) $Mode
    }
    $kaizoExit = $LASTEXITCODE
    Write-SetupLog 'INFO' ('PYTHON_EXIT code=' + $kaizoExit)
} catch {
    $kaizoError = Protect-LogText ('stage=' + $kaizoStage + ' type=' + $_.Exception.GetType().Name + ' HRESULT=0x' + $_.Exception.HResult.ToString('X8') + ' line=' + $_.InvocationInfo.ScriptLineNumber + ' target=' + [string]$_.TargetObject + ' message=' + $_.Exception.Message)
    try { Write-SetupLog 'ERROR' $kaizoError } catch { }
    Write-Host ('Setup could not continue: ' + $kaizoError) -ForegroundColor Red
} finally {
    if ($kaizoRuntime -and (Test-Path -LiteralPath $kaizoRuntime)) {
        try {
            Remove-Item -LiteralPath $kaizoRuntime -Recurse -Force
            Write-SetupLog 'INFO' 'RUNTIME_CLEANUP_DONE'
        } catch {
            $kaizoCleanupError = Protect-LogText $_.Exception.Message
            try { Write-SetupLog 'WARNING' ('RUNTIME_CLEANUP_FAILED ' + $kaizoCleanupError) } catch { }
            Write-Host ('Temporary runtime cleanup: ' + $kaizoCleanupError) -ForegroundColor Yellow
        }
    }
    try { Write-SetupLog 'INFO' ('END code=' + $kaizoExit + ' elapsed=' + [Math]::Round(((Get-Date) - $kaizoStarted).TotalSeconds, 1) + 's') } catch { }
    if ($kaizoLog) { Write-Host ('Log: ' + $kaizoLog) -ForegroundColor Cyan }
}
exit $kaizoExit
