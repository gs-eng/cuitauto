#Requires -Version 5.1
<#
    校园网自动登录 —— 安装开机自启
    直接用 Windows PowerShell 运行，或在资源管理器里右键“使用 PowerShell 运行”。
    不需要管理员权限。
#>

$ErrorActionPreference = 'Stop'

function Write-Step($t) {
    Write-Host ''
    Write-Host "── $t ──" -ForegroundColor Cyan
}

Write-Host '==================================================' -ForegroundColor Green
Write-Host '   校园网自动登录 —— 安装开机自启' -ForegroundColor Green
Write-Host '==================================================' -ForegroundColor Green

$root = $PSScriptRoot
if (-not $root) { $root = Split-Path -Parent $MyInvocation.MyCommand.Path }

$pyScript = Join-Path $root '校园网自动登录.py'
$cfgFile  = Join-Path $root '校园网配置.json'

if (-not (Test-Path -LiteralPath $pyScript)) {
    Write-Host "[×] 找不到主程序：$pyScript" -ForegroundColor Red
    Read-Host '按回车键退出'; exit 1
}

# ---------------------------------------------------------------- 1. 找 Python
Write-Step '第 1 步：查找 Python'

$pythonExe = $null
foreach ($cmd in @('py', 'python', 'python3')) {
    $c = Get-Command $cmd -ErrorAction SilentlyContinue
    if (-not $c) { continue }
    try {
        $out = & $c.Source -c "import sys;print(sys.executable)" 2>$null
        if ($out -and (Test-Path -LiteralPath ([string]$out).Trim())) {
            $pythonExe = ([string]$out).Trim()
            break
        }
    } catch { }
}

if (-not $pythonExe) {
    Write-Host '[×] 没有找到 Python，请先安装 Python 3 并勾选 “Add to PATH”。' -ForegroundColor Red
    Read-Host '按回车键退出'; exit 1
}

$pyDir   = Split-Path -Parent $pythonExe
$pythonw = Join-Path $pyDir 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) { $pythonw = $pythonExe }

Write-Host "  Python 解释器 : $pythonExe"
Write-Host "  后台运行用   : $pythonw"

# ---------------------------------------------------------------- 2. 检查配置
Write-Step '第 2 步：检查账号密码'

if (-not (Test-Path -LiteralPath $cfgFile)) {
    Write-Host "[×] 找不到配置文件：$cfgFile" -ForegroundColor Red
    Read-Host '按回车键退出'; exit 1
}

try {
    $cfg = Get-Content -LiteralPath $cfgFile -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    Write-Host "[×] 配置文件不是合法的 JSON，请检查：$cfgFile" -ForegroundColor Red
    Read-Host '按回车键退出'; exit 1
}

$userName = [string]$cfg.userName
if ([string]::IsNullOrWhiteSpace($userName) -or $userName -like '*在这里填*') {
    Write-Host '[!] 你还没有填账号密码。' -ForegroundColor Yellow
    Write-Host "    请先用记事本打开并填写：$cfgFile" -ForegroundColor Yellow
    Write-Host '    填好以后重新运行本脚本。' -ForegroundColor Yellow
    Read-Host '按回车键退出'; exit 1
}
Write-Host "  账号：$userName"

# ---------------------------------------------------------------- 3. 创建自启项
Write-Step '第 3 步：创建开机自启项'

$startup = [Environment]::GetFolderPath('Startup')
$lnk     = Join-Path $startup '校园网自动登录.lnk'
$runArgs = '"{0}" --daemon --quiet' -f $pyScript
$runKey  = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$runName = '校园网自动登录'
$method  = $null

# 首选：启动文件夹里的快捷方式（看得见、好管理）
try {
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($lnk)
    $sc.TargetPath       = $pythonw
    $sc.Arguments        = $runArgs
    $sc.WorkingDirectory = $root
    $sc.WindowStyle      = 1
    $sc.Description      = '校园网自动登录（后台守护）'
    $sc.Save()
    if (Test-Path -LiteralPath $lnk) {
        $method = 'startup'
        Write-Host '  方式：启动文件夹' -ForegroundColor Green
        Write-Host "  已创建：$lnk" -ForegroundColor Green
    }
} catch {
    Write-Host "  [i] 启动文件夹写不了（$($_.Exception.Message)），改用注册表。" -ForegroundColor Yellow
}

# 退路：注册表 Run 键
if (-not $method) {
    try {
        if (-not (Test-Path $runKey)) { New-Item -Path $runKey -Force | Out-Null }
        $cmd = '"{0}" "{1}" --daemon --quiet' -f $pythonw, $pyScript
        New-ItemProperty -Path $runKey -Name $runName -Value $cmd -PropertyType String -Force | Out-Null
        $method = 'registry'
        Write-Host '  方式：注册表启动项（启动文件夹不可写，已自动改用注册表）' -ForegroundColor Green
        Write-Host "  位置：HKCU\Software\Microsoft\Windows\CurrentVersion\Run\$runName" -ForegroundColor Green
    } catch {
        Write-Host "[×] 两种方式都没能创建自启项：$($_.Exception.Message)" -ForegroundColor Red
        Write-Host '    你可以手动把下面这条命令加到「任务计划程序」里实现开机运行：' -ForegroundColor Yellow
        Write-Host "    $pythonw `"$pyScript`" --daemon --quiet" -ForegroundColor Yellow
        Read-Host '按回车键退出'; exit 1
    }
}

# ---------------------------------------------------------------- 4. 立即启动一次
Write-Step '第 4 步：立即在后台启动'

$running = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
           Where-Object { $_.CommandLine -and $_.CommandLine -like '*校园网自动登录.py*' }

if ($running) {
    Write-Host '  已经有一个守护进程在运行了，先把它停掉再启动新的。'
    $running | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 700
}

# 注意：-ArgumentList 传数组时不会给含空格的路径加引号，必须自己加，
#       否则脚本路径会被空格切碎，进程直接起不来。
Start-Process -FilePath $pythonw -ArgumentList ('"{0}" --daemon --quiet' -f $pyScript) -WorkingDirectory $root
Start-Sleep -Seconds 3

$now = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
       Where-Object { $_.CommandLine -and $_.CommandLine -like '*校园网自动登录.py*' }

if ($now) {
    Write-Host "  已在后台运行（进程号：$($now.ProcessId -join ', ')）" -ForegroundColor Green
} else {
    Write-Host '  [!] 后台进程没有起来，请手动运行一次看看报错：' -ForegroundColor Yellow
    Write-Host "      py `"$pyScript`" --diagnose"
}

# ---------------------------------------------------------------- 完成
Write-Host ''
Write-Host '==================================================' -ForegroundColor Green
Write-Host '   安装完成' -ForegroundColor Green
Write-Host '==================================================' -ForegroundColor Green
Write-Host ''
Write-Host "  日志目录 : $(Join-Path $root '日志')"
Write-Host '  运行方式 : 每次登录 Windows 后自动在后台运行；'
Write-Host '             发现掉线会自动重新登录。'
Write-Host ''
Write-Host '  手动检查 : py "校园网自动登录.py" --check'
Write-Host '  取消自启 : 运行 卸载开机自启.ps1'
Write-Host ''
Read-Host '按回车键退出'
