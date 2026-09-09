function Get-OpenAIAppPackages {
    @(Get-AppxPackage -ErrorAction Stop | Where-Object {
        $_.Name -in @('OpenAI.Codex', 'OpenAI.ChatGPT') -and
        $_.PublisherId -eq '2p2nqsd0c76g0' -and -not $_.IsFramework -and -not $_.IsResourcePackage
    })
}

function Write-AppStatus([string]$Message, [string]$Color = 'Cyan') {
    Write-SetupLog 'INFO' ('APP_RECOVERY ' + $Message)
    Write-Host (Protect-LogText $Message) -ForegroundColor $Color
}

function Write-AppError($Record) {
    $detail = 'type=' + $Record.Exception.GetType().Name +
        ' HRESULT=0x' + $Record.Exception.HResult.ToString('X8') +
        ' id=' + $Record.FullyQualifiedErrorId + ' message=' + $Record.Exception.Message
    Write-SetupLog 'ERROR' ('APP_RECOVERY ' + $detail)
    Write-Host (Protect-LogText $detail) -ForegroundColor Red
    $message = $Record.ToString()
    $hint = if ($message -match '80073D02') {
        '应用或相关组件仍在使用。保存工作并退出应用，必要时重启 Windows 后再运行。'
    } elseif ($message -match '80073CF3') {
        'Windows 报告依赖或冲突问题。请在系统应用设置中修复应用，并检查 Windows 更新。'
    } elseif ($message -match '80073D06') {
        '本机已有更新版本；未强制降级。可改用重新注册或 Windows 设置中的修复。'
    } elseif ($message -match '80070005|Access.*denied|拒绝访问') {
        'Windows 拒绝访问。请保留日志，检查组织策略或安全软件；不要修改 WindowsApps 的所有权或权限。'
    } else {
        '请保留此日志；可先重启 Windows，再尝试 设置 → 应用 → ChatGPT/Codex → 高级选项 → 修复。'
    }
    Write-AppStatus $hint 'Yellow'
}

function Write-AppEvents {
    $since = (Get-Date).AddDays(-3)
    foreach ($channel in @('Application', 'Microsoft-Windows-AppModel-Runtime/Admin',
        'Microsoft-Windows-AppXDeploymentServer/Operational', 'Microsoft-Windows-TWinUI/Operational')) {
        try {
            # Bound collection, and never persist complete event messages or command lines.
            $events = @(Get-WinEvent -FilterHashtable @{LogName=$channel; StartTime=$since; Level=@(1,2,3)} -MaxEvents 100 -ErrorAction Stop)
            $matched = 0
            foreach ($event in $events) {
                $xml = [xml]$event.ToXml()
                if ($xml.OuterXml -notmatch 'OpenAI\.(Codex|ChatGPT)|\b(codex|chatgpt)\.exe\b') { continue }
                $matched++
                $codes = @([regex]::Matches($xml.OuterXml, '(?i)\b0x[0-9a-f]{8}\b') | ForEach-Object { $_.Value } | Select-Object -Unique)
                $fields = @()
                foreach ($field in @($xml.Event.EventData.Data)) {
                    if ($field.Name -in @('AppName','ModuleName','ExceptionCode','ErrorCode','Status')) {
                        $value = [string]$field.'#text'
                        if ($value -match '^[A-Za-z0-9_.-]{1,100}$') { $fields += ([string]$field.Name + '=' + $value) }
                    }
                }
                Write-SetupLog 'INFO' ('APP_EVENT time=' + $event.TimeCreated.ToString('o') +
                    ' channel=' + $channel + ' id=' + $event.Id + ' record=' + $event.RecordId +
                    ' codes=' + ($codes -join ',') + ' ' + ($fields -join ' '))
            }
            Write-SetupLog 'INFO' ('APP_EVENTS channel=' + $channel + ' scanned=' + $events.Count + ' matched=' + $matched + ' window=72h limit=100')
        } catch {
            if ($_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*') {
                Write-SetupLog 'INFO' ('APP_EVENTS channel=' + $channel + ' no-events-in-window')
            } else {
                Write-SetupLog 'WARNING' ('APP_EVENTS_UNAVAILABLE channel=' + $channel + ' HRESULT=0x' + $_.Exception.HResult.ToString('X8'))
            }
        }
    }
}

function Write-AppInventory($Packages) {
    Write-AppStatus '01 / 检查安装记录、系统服务与近期错误'
    Write-SetupLog 'INFO' ('APP_RECOVERY_VERSION 2.5.0 os=' + [Environment]::OSVersion.VersionString +
        ' arch=' + $env:PROCESSOR_ARCHITECTURE + ' nativeArch=' + $env:PROCESSOR_ARCHITEW6432)
    foreach ($package in $Packages) {
        Write-AppStatus ($package.Name + ' · ' + $package.Version + ' · 状态 ' + $package.Status)
        Write-SetupLog 'INFO' ('APP_PACKAGE fullName=' + $package.PackageFullName +
            ' family=' + $package.PackageFamilyName + ' location=' + $package.InstallLocation +
            ' signature=' + $package.SignatureKind + ' status=' + $package.Status)
        foreach ($dependency in @($package.Dependencies)) {
            if ($dependency) {
                Write-SetupLog 'INFO' ('APP_DEPENDENCY package=' + $dependency.PackageFullName + ' status=' + $dependency.Status)
            }
        }
    }
    foreach ($name in @('AppXSvc', 'ClipSVC', 'StateRepository', 'InstallService')) {
        try {
            $service = Get-Service -Name $name -ErrorAction Stop
            Write-SetupLog 'INFO' ('APP_SERVICE name=' + $name + ' status=' + $service.Status + ' startType=' + $service.StartType)
        } catch { Write-SetupLog 'WARNING' ('APP_SERVICE_UNAVAILABLE name=' + $name) }
    }
    foreach ($scope in @('Process', 'User', 'Machine')) {
        foreach ($name in @('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY')) {
            $present = -not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name, $scope))
            Write-SetupLog 'INFO' ('APP_PROXY scope=' + $scope + ' name=' + $name + ' present=' + $present)
        }
    }
    Write-AppEvents
    Write-AppStatus '诊断记录已保存。代理仅记录是否设置，账户、Key 和聊天正文不写入日志。' 'DarkGray'
}

function Assert-AppClosed {
    $running = @(Get-Process -Name Codex,ChatGPT -ErrorAction SilentlyContinue)
    if ($running.Count -gt 0) {
        Write-SetupLog 'WARNING' ('APP_RUNNING ids=' + (($running | ForEach-Object { $_.Id }) -join ','))
        throw 'ChatGPT/Codex 进程仍在运行。请保存工作并退出应用后重试；脚本不会强制结束进程。'
    }
}

function Register-OpenAIApp($Package) {
    Assert-AppClosed
    # Only re-register this user's selected, existing package. Never reset app data.
    if ([string]::IsNullOrWhiteSpace($Package.InstallLocation)) { throw '安装记录缺少目录；请改选官方 MSIX 安装。' }
    $manifest = Join-Path $Package.InstallLocation 'AppxManifest.xml'
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf -ErrorAction Stop)) {
        throw '已注册应用的 AppxManifest.xml 不存在；重新注册无法补回安装文件，请改选官方 MSIX 安装。'
    }
    Write-AppStatus ('02 / 重新注册 ' + $Package.Name + '，请稍候')
    Write-SetupLog 'INFO' ('APP_REGISTER_START manifest=' + $manifest)
    Add-AppxPackage -Path $manifest -Register -DisableDevelopmentMode -ErrorAction Stop | Out-Null
    Write-SetupLog 'INFO' 'APP_REGISTER_DONE'
    return $Package.Name
}

function Read-OpenAIPackageIdentity([string]$Path) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $entry = $zip.GetEntry('AppxManifest.xml')
        if (-not $entry -or $entry.Length -gt 1MB) { throw '下载文件缺少有效的 MSIX 清单，未安装。' }
        $reader = New-Object IO.StreamReader($entry.Open())
        $settings = New-Object Xml.XmlReaderSettings
        $settings.DtdProcessing = [Xml.DtdProcessing]::Prohibit
        $settings.XmlResolver = $null
        $settings.MaxCharactersInDocument = 1MB
        $xmlReader = [Xml.XmlReader]::Create($reader, $settings)
        try {
            $manifest = New-Object Xml.XmlDocument
            $manifest.XmlResolver = $null
            $manifest.Load($xmlReader)
        } finally { $xmlReader.Dispose(); $reader.Dispose() }
        $name = [string]$manifest.Package.Identity.Name
        if ($name -notin @('OpenAI.Codex','OpenAI.ChatGPT')) { throw '下载包的应用身份不符合预期，未安装。' }
        Write-SetupLog 'INFO' ('APP_DOWNLOAD_IDENTITY name=' + $name + ' version=' + $manifest.Package.Identity.Version)
        return $name
    } finally { $zip.Dispose() }
}

function Install-OpenAIAppDirect {
    Assert-AppClosed
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') { 'arm64' } else { 'x64' }
    $url = 'https://persistent.oaistatic.com/codex-app-prod/ChatGPT-' + $arch + '.msix'
    $cache = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'KAIZO-Setup\Downloads'
    [IO.Directory]::CreateDirectory($cache) | Out-Null
    $path = Join-Path $cache ('ChatGPT-' + [Guid]::NewGuid().ToString('N') + '.msix')
    try {
        Write-AppStatus ('02 / 下载官方 ' + $arch + ' 安装包；下载进度显示在下方')
        Write-SetupLog 'INFO' ('APP_DOWNLOAD_START url=' + $url + ' path=' + $path)
        & "$env:SystemRoot\System32\curl.exe" -q --fail --location --progress-bar --show-error --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 900 --output $path $url
        if ($LASTEXITCODE -ne 0) { throw ('官方下载失败，curl 退出码 ' + $LASTEXITCODE + '。请检查当前终端代理和网络。') }
        Write-SetupLog 'INFO' ('APP_DOWNLOAD_DONE sha256=' + (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash)
        $name = Read-OpenAIPackageIdentity $path
        Assert-AppClosed
        Write-AppStatus '03 / 安装官方 MSIX；Windows 将验证包签名和依赖'
        Add-AppxPackage -Path $path -ErrorAction Stop | Out-Null
        Write-SetupLog 'INFO' 'APP_INSTALL_DONE'
        return $name
    } finally {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue }
    }
}

function Assert-AppRecoveryUser {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw '请关闭本窗口，用日常 Windows 账户普通双击 Repair-App.cmd；不要以管理员身份运行。'
    }
}

function Invoke-AppRecovery([switch]$DirectInstall) {
    Write-Host ''
    Write-Host '  KAIZO / ChatGPT · Codex 应用恢复  v2.5.0' -ForegroundColor Cyan
    Write-Host '  保留账户、第三方配置和聊天数据；不自动启动应用。' -ForegroundColor DarkGray
    Assert-AppRecoveryUser
    $packages = @(Get-OpenAIAppPackages)
    Write-AppInventory $packages
    $choice = '2'
    if (-not $DirectInstall) {
        Write-Host ''
        Write-Host '  1  重新注册已安装应用（不下载）' -ForegroundColor White
        Write-Host '  2  从 OpenAI 下载官方 MSIX 并安装（绕过商店下载服务）' -ForegroundColor White
        Write-Host '  3  仅保存诊断日志' -ForegroundColor White
        Write-Host '  0  退出' -ForegroundColor DarkGray
        do { $choice = Read-Host '选择 1 / 2 / 3 / 0' } while ($choice -notin @('0','1','2','3'))
    }
    Write-SetupLog 'INFO' ('APP_CHOICE ' + $choice)
    if ($choice -in @('0','3')) {
        Write-AppStatus '诊断结束，尚未执行修复。' 'Yellow'
        return 0
    }
    try {
        if ($choice -eq '1') {
            if ($packages.Count -eq 0) { throw '当前用户没有找到支持的 OpenAI 应用注册记录。请重新运行并选择 2。' }
            $selected = $packages[0]
            if ($packages.Count -gt 1) {
                for ($i=0; $i -lt $packages.Count; $i++) {
                    Write-Host ('  ' + ($i+1) + '  ' + $packages[$i].Name + ' ' + $packages[$i].Version)
                }
                $number = 0
                do { $answer = Read-Host '存在多条安装记录，请选中要修复的一条' }
                while (-not [int]::TryParse($answer, [ref]$number) -or $number -lt 1 -or $number -gt $packages.Count)
                $selected = $packages[$number-1]
            }
            $name = Register-OpenAIApp $selected
        } else { $name = Install-OpenAIAppDirect }
        $after = @(Get-OpenAIAppPackages | Where-Object { $_.Name -eq $name })
        if ($after.Count -ne 1 -or [string]$after[0].Status -ne 'Ok') {
            throw '操作返回后，应用注册状态仍未确认正常。请把本次日志用于继续诊断。'
        }
        Write-SetupLog 'INFO' ('APP_READBACK fullName=' + $after[0].PackageFullName + ' status=' + $after[0].Status)
        Write-AppStatus '操作完成，Windows 注册状态正常；应用能否打开尚未验证。' 'Green'
        Write-AppStatus '请从开始菜单手动打开 ChatGPT/Codex。若仍打不开，重跑本脚本选 3，保留最新日志。' 'Cyan'
        return 0
    } catch {
        Write-AppError $_
        Write-AppEvents
        Write-AppStatus '本次操作未完成，不能视为已修复。' 'Red'
        return 1
    }
}
