$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$tokens = $null; $errors = $null
$bootstrap = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $repo 'bootstrap.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($fn in $bootstrap.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $true)) {
    . ([scriptblock]::Create($fn.Extent.Text))
}
$recoveryPath = Join-Path $PSScriptRoot 'app-recovery.ps1'
$recovery = [System.Management.Automation.Language.Parser]::ParseFile($recoveryPath, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
. $recoveryPath
# No installed applications, native package changes, or network requests in this check.
$temp = Join-Path ([IO.Path]::GetTempPath()) ('kaizo-recovery-check-' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($temp) | Out-Null
$kaizoLog = Join-Path $temp 'check.log'
$script:registerCalls = @()
$script:running = @()
function Get-Process { param($Name, $ErrorAction) $script:running }
function Add-AppxPackage {
    param($Path, [switch]$Register, [switch]$DisableDevelopmentMode, $ErrorAction)
    $script:registerCalls += @{Path=$Path; Register=$Register; DisableDevelopmentMode=$DisableDevelopmentMode}
    if ($script:denyRegistration) { throw 'HRESULT 0x80070005 fixture access denied sk-fixture-secret' }
}
function Get-AppxPackage {
    param($ErrorAction)
    @(
        [pscustomobject]@{Name='OpenAI.Codex'; PublisherId='2p2nqsd0c76g0'; IsFramework=$false; IsResourcePackage=$false},
        [pscustomobject]@{Name='OpenAI.ChatGPT'; PublisherId='2p2nqsd0c76g0'; IsFramework=$false; IsResourcePackage=$false},
        [pscustomobject]@{Name='OpenAI.Codex'; PublisherId='untrusted'; IsFramework=$false; IsResourcePackage=$false},
        [pscustomobject]@{Name='Unrelated.App'; PublisherId='2p2nqsd0c76g0'; IsFramework=$false; IsResourcePackage=$false}
    )
}
function Get-WinEvent {
    param($FilterHashtable, $MaxEvents, $ErrorAction)
    if ($MaxEvents -ne 100 -or -not $FilterHashtable.StartTime) { throw 'Event collection is not bounded.' }
    $sample = [pscustomobject]@{TimeCreated=(Get-Date); Id=1000; RecordId=123}
    $sample | Add-Member -MemberType ScriptMethod -Name ToXml -Value {
        '<Event><EventData><Data Name="AppName">Codex.exe</Data><Data Name="ModuleName">module.dll</Data><Data Name="ExceptionCode">0xc0000005</Data><Data Name="CommandLine">sk-fixture-event-secret Bearer fixture-token</Data></EventData></Event>'
    }
    $sample
}
try {
    $packages = @(Get-OpenAIAppPackages)
    if ($packages.Count -ne 2) { throw 'Application identity or publisher filtering failed.' }
    $manifest = Join-Path $temp 'AppxManifest.xml'
    [IO.File]::WriteAllText($manifest, '<Package/>')
    $package = [pscustomobject]@{Name='OpenAI.Codex'; InstallLocation=$temp}
    $name = Register-OpenAIApp $package
    if ($name -ne 'OpenAI.Codex' -or $script:registerCalls.Count -ne 1) { throw 'Registration did not run exactly once.' }
    $call = $script:registerCalls[0]
    if ($call.Path -ne $manifest -or -not $call.Register -or -not $call.DisableDevelopmentMode) { throw 'Registration target or mode is incorrect.' }
    $script:running = @([pscustomobject]@{Id=42})
    $blocked = $false
    try { Register-OpenAIApp $package | Out-Null } catch { $blocked = $true }
    if (-not $blocked -or $script:registerCalls.Count -ne 1) { throw 'Running application was not protected.' }
    $script:running = @()
    Remove-Item -LiteralPath $manifest
    $blocked = $false
    try { Register-OpenAIApp $package | Out-Null } catch { $blocked = $true }
    if (-not $blocked -or $script:registerCalls.Count -ne 1) { throw 'Missing manifest was not handled safely.' }
    [IO.File]::WriteAllText($manifest, '<Package/>')
    $script:denyRegistration = $true
    $blocked = $false
    try { Register-OpenAIApp $package | Out-Null } catch { $blocked = $true; Write-AppError $_ }
    if (-not $blocked) { throw 'Permission failure was swallowed.' }
    Write-AppEvents
    $logged = Get-Content -LiteralPath $kaizoLog -Raw
    if ($logged -match 'fixture-(secret|event-secret|token)') { throw 'Recovery logs leaked fixture credentials.' }
    if ($logged -notmatch '80070005' -or $logged -notmatch '0xc0000005' -or $logged -notmatch 'ModuleName=module.dll') { throw 'Useful error diagnostics were lost.' }
    $commands = $recovery.FindAll({param($node) $node -is [System.Management.Automation.Language.CommandAst]}, $true)
    foreach ($command in $commands) {
        if ($command.GetCommandName() -in @('Reset-AppxPackage','Remove-AppxPackage','Stop-Process','Start-Process','Invoke-Expression')) { throw 'Recovery contains a destructive or application-launch command.' }
        if ($command.Extent.Text -match '(?i)-(AllUsers|ForceApplicationShutdown|ForceTargetApplicationShutdown|ForceUpdateFromAnyVersion|AllowUnsigned)\b') { throw 'Recovery contains an unsafe package option.' }
    }
    $bootstrapText = [IO.File]::ReadAllText((Join-Path $repo 'bootstrap.ps1'))
    $dispatch = $bootstrapText.IndexOf("if (`$Mode -in @('app-recovery','app-install'))")
    if ($dispatch -lt 0 -or $dispatch -gt $bootstrapText.IndexOf("`$kaizoStage = 'verify-runtime'")) { throw 'App recovery unnecessarily depends on the Python bootstrap.' }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archiveDir = Join-Path $temp 'archive'
    [IO.Directory]::CreateDirectory($archiveDir) | Out-Null
    foreach ($identity in @('OpenAI.Codex','OpenAI.ChatGPT','Unrelated.App')) {
        [IO.File]::WriteAllText((Join-Path $archiveDir 'AppxManifest.xml'), ('<Package><Identity Name="' + $identity + '" Version="1.0.0.0"/></Package>'))
        $zipPath = Join-Path $temp ($identity + '.msix')
        [IO.Compression.ZipFile]::CreateFromDirectory($archiveDir, $zipPath)
        $blocked = $false
        try { $readName = Read-OpenAIPackageIdentity $zipPath } catch { $blocked = $true }
        if ($identity -eq 'Unrelated.App') {
            if (-not $blocked) { throw 'Unrelated downloaded package was accepted.' }
        } elseif ($blocked -or $readName -ne $identity) { throw 'Supported package identity was lost.' }
    }
    # Run the real top-level flow with only native I/O and user input replaced.
    function Assert-AppRecoveryUser { }
    function Write-AppInventory { param($Packages) }
    function Get-OpenAIAppPackages {
        if ($script:installed) { [pscustomobject]@{Name='OpenAI.Codex'; Status='Ok'; PackageFullName='fixture'} }
    }
    function Install-OpenAIAppDirect {
        if ($script:installDenied) { throw '0x80070005 access denied' }
        $script:installed = $true
        return 'OpenAI.Codex'
    }
    function Read-Host { throw 'Direct install must not ask for a menu selection.' }
    if ((Invoke-AppRecovery -DirectInstall) -ne 0) { throw 'Fresh direct install did not complete.' }
    $script:installDenied = $true
    if ((Invoke-AppRecovery -DirectInstall) -ne 1) { throw 'Failed install was reported as success.' }
    Write-Output 'PASS: targeted package registration, active-app protection, missing manifest, permission errors, bounded and private event diagnostics; no real application or network used.'
} finally {
    Remove-Item -LiteralPath $temp -Recurse -Force
}
