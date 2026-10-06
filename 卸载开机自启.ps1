#Requires -Version 5.1
<#
    校园网自动登录 —— 卸载开机自启
    运行后会删除启动项，并结束正在后台运行的守护进程。
    程序文件和日志不会被删除。
#>

$ErrorActionPreference = 'Continue'

Write-Host '==================================================' -ForegroundColor Yellow
Write-Host '   校园网自动登录 —— 卸载开机自启' -ForegroundColor Yellow
Write-Host '==================================================' -ForegroundColor Yellow

# ---------------------------------------------------------------- 1. 删启动项
Write-Host ''
Write-Host '── 第 1 步：删除开机启动项 ──' -ForegroundColor Cyan

$startup = [Environment]::GetFolderPath('Startup')
$lnk     = Join-Path $startup '校园网自动登录.lnk'
$runKey  = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$runName = '校园网自动登录'
$found   = $false

if (Test-Path -LiteralPath $lnk) {
    try {
        Remove-Item -LiteralPath $lnk -Force -ErrorAction Stop
        Write-Host "  已删除启动文件夹快捷方式：$lnk" -ForegroundColor Green
        $found = $true
    } catch {
        Write-Host "  删除快捷方式失败：$($_.Exception.Message)" -ForegroundColor Yellow
    }
}

try {
    $prop = Get-ItemProperty -Path $runKey -Name $runName -ErrorAction SilentlyContinue
    if ($prop) {
        Remove-ItemProperty -Path $runKey -Name $runName -Force -ErrorAction Stop
        Write-Host "  已删除注册表启动项：HKCU\...\Run\$runName" -ForegroundColor Green
        $found = $true
    }
} catch {
    Write-Host "  清理注册表启动项失败：$($_.Exception.Message)" -ForegroundColor Yellow
}

if (-not $found) {
    Write-Host '  没有找到自启项（可能之前已经卸载过了）。'
}

# ---------------------------------------------------------------- 2. 结束进程
Write-Host ''
Write-Host '── 第 2 步：结束后台守护进程 ──' -ForegroundColor Cyan

$procs = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
         Where-Object { $_.CommandLine -and $_.CommandLine -like '*校园网自动登录.py*' }

if ($procs) {
    foreach ($p in $procs) {
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
            Write-Host "  已结束进程 $($p.ProcessId)" -ForegroundColor Green
        } catch {
            Write-Host "  结束进程 $($p.ProcessId) 失败：$($_.Exception.Message)" -ForegroundColor Yellow
        }
    }
} else {
    Write-Host '  没有正在运行的守护进程。'
}

# ---------------------------------------------------------------- 完成
Write-Host ''
Write-Host '==================================================' -ForegroundColor Green
Write-Host '   已卸载完成，开机不会再自动运行。' -ForegroundColor Green
Write-Host '==================================================' -ForegroundColor Green
Write-Host ''
Write-Host '  程序文件和日志都保留着，需要时可以重新运行 安装开机自启.ps1'
Write-Host ''
Read-Host '按回车键退出'
