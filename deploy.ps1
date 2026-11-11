# imghost 部署脚本 —— 以管理员身份运行
# 把图床安装到指定目录，注册为开机自启，放行防火墙端口，并做一次健康检查。
#
# 用法示例：
#   powershell -ExecutionPolicy Bypass -File deploy.ps1
#   powershell -ExecutionPolicy Bypass -File deploy.ps1 -DeployDir D:\imghost -Port 8899
#   powershell -ExecutionPolicy Bypass -File deploy.ps1 -Uninstall
param(
    [string]$DeployDir = 'D:\imghost',
    [int]$Port = 8899,
    [string]$Bind = '0.0.0.0',
    [string]$ImageDir = '',
    [string]$TaskName = 'imghost',
    [switch]$AtLogon,
    [switch]$NoAutostart,
    [switch]$NoFirewall,
    [switch]$Uninstall,
    [switch]$NoPause
)

$ErrorActionPreference = 'Stop'
$scriptDir = $PSScriptRoot
$scriptFile = Join-Path $scriptDir 'imghost.py'

if (-not $ImageDir) {
    $ImageDir = Join-Path $DeployDir 'images'
}

# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Write-Ok([string]$Text) {
    Write-Host "    $Text" -ForegroundColor Green
}

function Write-Warn([string]$Text) {
    Write-Host "    $Text" -ForegroundColor Yellow
}

function Get-LanIPv4 {
    try {
        $candidate = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object {
                $_.IPAddress -notlike '127.*' -and
                $_.IPAddress -notlike '169.254.*' -and
                $_.IPAddress -ne '0.0.0.0' -and
                $_.PrefixOrigin -ne 'WellKnown'
            } |
            Sort-Object -Property SkipAsSource, InterfaceMetric |
            Select-Object -First 1
        if ($null -ne $candidate) { return $candidate.IPAddress }
    }
    catch {
    }
    return '127.0.0.1'
}

# 找一个可用的 Python 解释器（3.8 及以上），返回 python.exe 的完整路径
function Resolve-Python {
    $candidates = New-Object System.Collections.Generic.List[string]

    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -ne $cmd) { $candidates.Add($cmd.Source) }

    $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -ne $pyLauncher) {
        try {
            $resolved = & $pyLauncher.Source -3 -c "import sys; print(sys.executable)" 2>$null
            if ($resolved) { $candidates.Add(($resolved | Select-Object -First 1).Trim()) }
        }
        catch {
        }
    }

    $searchRoots = @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles", "${env:ProgramFiles(x86)}")
    foreach ($root in $searchRoots) {
        if (-not $root -or -not (Test-Path $root)) { continue }
        try {
            $found = Get-ChildItem -Path $root -Filter 'python.exe' -Recurse -Depth 2 -ErrorAction SilentlyContinue |
                Where-Object { $_.DirectoryName -match 'Python3' } |
                Select-Object -ExpandProperty FullName
            foreach ($f in $found) { $candidates.Add($f) }
        }
        catch {
        }
    }

    foreach ($path in ($candidates | Select-Object -Unique)) {
        if (-not $path -or -not (Test-Path $path)) { continue }
        try {
            $version = & $path -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if (-not $version) { continue }
            $parts = $version.Trim().Split('.')
            $major = [int]$parts[0]
            $minor = [int]$parts[1]
            if ($major -eq 3 -and $minor -ge 8) {
                return (Resolve-Path $path).Path
            }
        }
        catch {
        }
    }
    return $null
}

# 停掉占用该端口的 python 进程（只动 python，不动别人）
function Stop-ImghostListener {
    param([int]$ListenPort)

    $killed = $false
    $connections = Get-NetTCPConnection -LocalPort $ListenPort -State Listen -ErrorAction SilentlyContinue
    foreach ($connection in $connections) {
        $ownerId = $connection.OwningProcess
        if (-not $ownerId) { continue }
        $owner = Get-Process -Id $ownerId -ErrorAction SilentlyContinue
        if ($null -eq $owner) { continue }
        if ($owner.ProcessName -notmatch '^python') {
            Write-Warn "端口 $ListenPort 被 $($owner.ProcessName) (PID $ownerId) 占用，不是图床进程，跳过。"
            continue
        }
        try {
            Stop-Process -Id $ownerId -Force -ErrorAction Stop
            Write-Ok "已停止占用端口的进程 PID $ownerId"
            $killed = $true
        }
        catch {
            Write-Warn "停止 PID $ownerId 失败：$($_.Exception.Message)"
        }
    }
    if ($killed) {
        Start-Sleep -Milliseconds 800
    }
}

function Wait-ImghostHealthy {
    param([int]$ListenPort, [int]$TimeoutSeconds = 30)

    $url = "http://127.0.0.1:$ListenPort/"
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) { return $true }
        }
        catch {
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

# 注册计划任务；Mode 为 Startup（开机、SYSTEM）或 Logon（当前用户登录后）
function Register-ImghostTask {
    param(
        [string]$Mode,
        [string]$Name,
        [string]$Exe,
        [string]$Arguments,
        [string]$WorkDir
    )

    $action = New-ScheduledTaskAction -Execute $Exe -Argument $Arguments -WorkingDirectory $WorkDir

    if ($Mode -eq 'Startup') {
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    }
    else {
        $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
    }

    $settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew

    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
}

function Unregister-ImghostTask {
    param([string]$Name)

    $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($null -eq $task) { return $false }
    try {
        Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    }
    catch {
    }
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction Stop
    return $true
}

# ---------------------------------------------------------------------------
# 卸载分支
# ---------------------------------------------------------------------------
$exitCode = 0
try {
    if ($Uninstall) {
        if (-not (Test-Administrator)) {
            $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-Uninstall')
            if ($DeployDir) { $arguments += @('-DeployDir', "`"$DeployDir`"") }
            if ($TaskName) { $arguments += @('-TaskName', "`"$TaskName`"") }
            try {
                Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $arguments | Out-Null
            }
            catch {
                Write-Host '需要管理员权限才能删除开机自启任务。' -ForegroundColor Red
            }
            exit 0
        }

        Write-Step "卸载图床自启"
        if (Unregister-ImghostTask -Name $TaskName) {
            Write-Ok "已删除计划任务 $TaskName"
        }
        else {
            Write-Warn "没有找到计划任务 $TaskName"
        }

        Stop-ImghostListener -ListenPort $Port

        $ruleName = "imghost $Port"
        if (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue) {
            Remove-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
            Write-Ok "已删除防火墙规则 $ruleName"
        }

        Write-Host ""
        Write-Host "卸载完成。图片目录仍然保留：$ImageDir" -ForegroundColor Green
        Write-Host "确认不需要了再手动删除 $DeployDir"
        exit 0
    }

    # -----------------------------------------------------------------------
    # 1. 检查运行环境
    # -----------------------------------------------------------------------
    Write-Step "检查运行环境"

    if (-not (Test-Path $scriptFile)) {
        throw "同目录下找不到 imghost.py：$scriptFile"
    }

    $isAdmin = Test-Administrator
    if ($isAdmin) {
        Write-Ok "管理员权限：有"
    }
    else {
        Write-Warn "管理员权限：无（无法配置开机自启和防火墙）"
    }

    $python = Resolve-Python
    if ($null -eq $python) {
        throw "没有找到 Python 3.8+。请先安装 Python 3（安装时勾选 Add python.exe to PATH），再重新运行本脚本。"
    }
    $pythonVersion = (& $python -c "import sys; print(sys.version.split()[0])").Trim()
    Write-Ok "Python：$pythonVersion  ($python)"

    $pythonw = Join-Path (Split-Path $python -Parent) 'pythonw.exe'
    if (Test-Path $pythonw) {
        Write-Ok "无窗口模式：pythonw.exe 可用"
    }
    else {
        $pythonw = $python
        Write-Warn "没找到 pythonw.exe，自启时会带一个命令行窗口"
    }

    # 端口占用检查（可能有别的程序占着）
    $existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($connection in $existing) {
        $owner = Get-Process -Id $connection.OwningProcess -ErrorAction SilentlyContinue
        if ($null -ne $owner -and $owner.ProcessName -notmatch '^python') {
            throw "端口 $Port 正被 $($owner.ProcessName) (PID $($owner.Id)) 占用，请换一个端口：-Port 8900"
        }
    }

    # -----------------------------------------------------------------------
    # 2. 停止旧实例
    # -----------------------------------------------------------------------
    Write-Step "停止旧实例"
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        try {
            Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            Write-Ok "已停止计划任务 $TaskName"
        }
        catch {
        }
        Start-Sleep -Milliseconds 500
    }
    Stop-ImghostListener -ListenPort $Port
    Write-Ok "端口 $Port 已空闲"

    # -----------------------------------------------------------------------
    # 3. 复制文件
    # -----------------------------------------------------------------------
    Write-Step "安装到 $DeployDir"

    $deployPath = [System.IO.Path]::GetFullPath($DeployDir)
    $inPlace = ($deployPath.TrimEnd('\') -ieq $scriptDir.TrimEnd('\'))

    if ($inPlace) {
        Write-Ok "部署目录就是当前目录，跳过复制"
    }
    else {
        New-Item -ItemType Directory -Path $DeployDir -Force | Out-Null
        $filesToCopy = @('imghost.py', 'README.md', '启动图床.cmd')
        foreach ($fileName in $filesToCopy) {
            $source = Join-Path $scriptDir $fileName
            if (-not (Test-Path $source)) {
                Write-Warn "跳过不存在的文件：$fileName"
                continue
            }
            Copy-Item -Path $source -Destination (Join-Path $DeployDir $fileName) -Force
            Write-Ok "已复制 $fileName"
        }
        $scriptFile = Join-Path $DeployDir 'imghost.py'
    }

    New-Item -ItemType Directory -Path $ImageDir -Force | Out-Null
    Write-Ok "图片目录：$ImageDir"

    # 语法自检，避免把一个坏文件设成开机自启
    $syntaxCheck = & $python -c "import ast, io, sys; ast.parse(io.open(sys.argv[1], encoding='utf-8').read()); print('ok')" $scriptFile 2>&1
    if ($LASTEXITCODE -ne 0 -or $syntaxCheck -notmatch 'ok') {
        throw "imghost.py 语法检查没通过：$syntaxCheck"
    }
    Write-Ok "语法检查通过"

    # -----------------------------------------------------------------------
    # 4. 配置开机自启
    # -----------------------------------------------------------------------
    $taskMode = ''
    if ($NoAutostart) {
        Write-Step "跳过开机自启（-NoAutostart），改为直接启动"
        if ($isAdmin) {
            Write-Warn "没有自启，重启电脑后需要再运行一次本脚本"
        }
    }
    elseif (-not $isAdmin) {
        Write-Step "没有管理员权限，改为写入当前用户的启动文件夹"
        $startupDir = [Environment]::GetFolderPath('Startup')
        $shortcutPath = Join-Path $startupDir "imghost.lnk"
        $arguments = "`"$scriptFile`" --port $Port --bind $Bind --dir `"$ImageDir`""
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $pythonw
        $shortcut.Arguments = $arguments
        $shortcut.WorkingDirectory = (Split-Path $scriptFile -Parent)
        $shortcut.WindowStyle = 7
        $shortcut.Description = 'imghost 内部图床'
        $shortcut.Save()
        Write-Ok "已创建启动快捷方式：$shortcutPath"
        $taskMode = 'StartupFolder'
    }
    else {
        Write-Step "配置开机自启"
        Unregister-ImghostTask -Name $TaskName | Out-Null

        $taskArguments = "`"$scriptFile`" --port $Port --bind $Bind --dir `"$ImageDir`""
        $workDir = Split-Path $scriptFile -Parent

        # 依次尝试：计划任务（会按需降级）→ 启动文件夹
        $taskMode = ''
        $order = @()
        if ($AtLogon) {
            $order += 'Logon'
        }
        else {
            $order += 'Startup'
            $order += 'Logon'
        }

        foreach ($mode in $order) {
            if ($taskMode) { continue }
            try {
                Register-ImghostTask -Mode $mode -Name $TaskName -Exe $pythonw `
                    -Arguments $taskArguments -WorkDir $workDir
                $taskMode = $mode
            }
            catch {
                $reason = $_.Exception.Message.Trim()
                if ($mode -eq 'Startup') {
                    Write-Warn "开机自启（SYSTEM）没成功：$reason"
                    Write-Warn "退回到「登录后启动」再试一次..."
                }
                else {
                    Write-Warn "计划任务也没成功：$reason"
                }
            }
        }

        if ($taskMode) {
            if ($taskMode -eq 'Startup') {
                Write-Ok "计划任务 $TaskName 已配置：开机自动启动（SYSTEM 账户，不需要登录）"
            }
            else {
                Write-Ok "计划任务 $TaskName 已配置：当前用户登录后自动启动"
            }
        }
        else {
            # 计划任务完全不可用时的兜底：写当前用户的启动文件夹
            Write-Step "改用当前用户的启动文件夹"
            try {
                $startupDir = [Environment]::GetFolderPath('Startup')
                $shortcutPath = Join-Path $startupDir 'imghost.lnk'
                $shell = New-Object -ComObject WScript.Shell
                $shortcut = $shell.CreateShortcut($shortcutPath)
                $shortcut.TargetPath = $pythonw
                $shortcut.Arguments = $taskArguments
                $shortcut.WorkingDirectory = $workDir
                $shortcut.WindowStyle = 7
                $shortcut.Description = 'imghost 内部图床'
                $shortcut.Save()
                Write-Ok "已创建启动快捷方式：$shortcutPath"
                Write-Ok "效果：当前用户登录后自动启动（不会开机即起）"
                $taskMode = 'StartupFolder'
            }
            catch {
                Write-Warn "启动文件夹也写不进去：$($_.Exception.Message.Trim())"
                Write-Warn "这台机器上没能配置自启，每次重启后手动双击「启动图床.cmd」即可"
            }
        }
    }

    # -----------------------------------------------------------------------
    # 5. 防火墙
    # -----------------------------------------------------------------------
    if ($NoFirewall) {
        Write-Step "跳过防火墙配置（-NoFirewall）"
    }
    elseif (-not $isAdmin) {
        Write-Step "没有管理员权限，跳过防火墙配置"
        Write-Warn "同事打不开的话，用管理员命令行执行："
        Write-Warn "netsh advfirewall firewall add rule name=`"imghost $Port`" dir=in action=allow protocol=TCP localport=$Port"
    }
    else {
        Write-Step "放行防火墙端口"
        $ruleName = "imghost $Port"
        $rule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
        if ($null -ne $rule) {
            Write-Ok "入站规则已存在：$ruleName"
        }
        else {
            $firewallOk = $false
            try {
                New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP `
                    -LocalPort $Port -Action Allow -Profile Any -ErrorAction Stop | Out-Null
                $firewallOk = $true
            }
            catch {
                Write-Warn "PowerShell 建规则失败：$($_.Exception.Message.Trim())"
            }

            if (-not $firewallOk) {
                try {
                    & netsh advfirewall firewall add rule name="$ruleName" dir=in action=allow protocol=TCP localport=$Port | Out-Null
                    if ($LASTEXITCODE -eq 0) { $firewallOk = $true }
                }
                catch {
                }
            }

            if ($firewallOk) {
                Write-Ok "已新增入站规则 $ruleName"
            }
            else {
                Write-Warn "没能自动放行端口，同事可能打不开。请用管理员命令行手动执行："
                Write-Warn "netsh advfirewall firewall add rule name=`"$ruleName`" dir=in action=allow protocol=TCP localport=$Port"
            }
        }
    }

    # -----------------------------------------------------------------------
    # 6. 启动并做健康检查
    # -----------------------------------------------------------------------
    Write-Step "启动图床"

    $started = $false
    if ($taskMode -eq 'Startup' -or $taskMode -eq 'Logon') {
        try {
            Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
            $started = $true
            Write-Ok "已通过计划任务启动"
        }
        catch {
            Write-Warn "通过计划任务启动失败：$($_.Exception.Message)"
        }
    }

    if (-not $started) {
        # 直接在当前会话拉起一个后台进程
        $arguments = "`"$scriptFile`" --port $Port --bind $Bind --dir `"$ImageDir`""
        $process = Start-Process -FilePath $pythonw -ArgumentList $arguments `
            -WorkingDirectory (Split-Path $scriptFile -Parent) -PassThru -WindowStyle Hidden
        Write-Ok "已直接启动后台进程 PID $($process.Id)"
    }

    Write-Host "    等待服务就绪..." -NoNewline
    $healthy = Wait-ImghostHealthy -ListenPort $Port -TimeoutSeconds 30
    Write-Host ""

    if (-not $healthy) {
        Write-Host ""
        Write-Host "服务没能正常响应 http://127.0.0.1:$Port/" -ForegroundColor Red
        if ($taskMode -eq 'Startup') {
            Write-Host "可能是 SYSTEM 账户读不到 $pythonw 所在位置（Python 装在用户目录下）。" -ForegroundColor Yellow
            Write-Host "改成登录自启再试一次：" -ForegroundColor Yellow
            Write-Host "    powershell -ExecutionPolicy Bypass -File deploy.ps1 -AtLogon"
        }
        Write-Host ""
        Write-Host "排查办法：手动运行下面这条命令，看输出的报错信息" -ForegroundColor Yellow
        Write-Host "    `"$python`" `"$scriptFile`" --port $Port --bind $Bind --dir `"$ImageDir`""
        throw "健康检查失败"
    }

    Write-Ok "健康检查通过"

    # -----------------------------------------------------------------------
    # 7. 汇总
    # -----------------------------------------------------------------------
    $lan = Get-LanIPv4
    Write-Host ""
    Write-Host "  ┌────────────────────────────────────────────────────────┐" -ForegroundColor Green
    Write-Host "  │  部署完成                                              │" -ForegroundColor Green
    Write-Host "  └────────────────────────────────────────────────────────┘" -ForegroundColor Green
    Write-Host ""
    Write-Host "    本机访问 :  http://127.0.0.1:$Port"
    Write-Host "    局域网   :  http://${lan}:$Port"
    Write-Host "    程序位置 :  $scriptFile"
    Write-Host "    图片目录 :  $ImageDir"
    Write-Host ""

    switch ($taskMode) {
        'Startup' { Write-Host "    开机自启 :  已启用（计划任务 $TaskName，开机即启动，不需要登录）" }
        'Logon' { Write-Host "    开机自启 :  已启用（计划任务 $TaskName，登录后启动）" }
        'StartupFolder' { Write-Host "    开机自启 :  已启用（当前用户启动文件夹里的 imghost.lnk）" }
        default { Write-Host "    开机自启 :  未启用（手动启动）" }
    }

    Write-Host ""
    Write-Host "    卸载方法 :  管理员命令行运行  deploy.ps1 -Uninstall"
    Write-Host "    备份方法 :  直接复制 $ImageDir 整个目录"
    Write-Host ""
}
catch {
    Write-Host ""
    Write-Host $_.Exception.Message -ForegroundColor Red
    $exitCode = 1
}

if (-not $NoPause -and [Environment]::UserInteractive) {
    Read-Host '按回车关闭'
}
exit $exitCode
