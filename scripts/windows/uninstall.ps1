<#
.SYNOPSIS
  minedocscan 지우기 (tasks/0009 4.5 라) — 프로그램만 지운다.

.DESCRIPTION
  작업 스케줄러의 minedocscan-serve 를 멈추고 지운다, 바탕 화면의 바로 가기, InstallDir 안의 앱 전용 파이썬(python\)·로그(logs\)·VERSION,
  사용자 PATH 의 그 항목, 사용자 환경 변수 MINEDOCSCAN_CONFIG.
  건드리지 않는 것 — 설정 파일(%APPDATA%\minedocscan), 사이트 팩·보관 폴더·작업 폴더·접수 폴더·엑셀 폴더. 어디에 있는지 찍는다.
  -RemoveConfig 면 install.ps1 이 쓴 설정 파일(%APPDATA%\minedocscan\minedocscan.toml)도 지운다 (다른 자리의 -Config 파일은 지우지 않는다).
  InstallDir 에 다른 것이 있으면(데이터를 그 안에 두었다) 그것은 남기고 폴더도 남긴다. -InstallDir 를 주지 않으면 작업 스케줄러의 작업이
  가리키는 폴더(없으면 기본 자리)다. minedocscan 의 설치 폴더가 아니면(VERSION·앱 전용 파이썬 안의 minedocscan 이 없다) 지우지 않는다.
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'minedocscan'),
    [switch]$RemoveConfig,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$TaskName = 'minedocscan-serve'
function Say([string]$text) { if (-not $Quiet) { Write-Host $text } }

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $PSBoundParameters.ContainsKey('InstallDir') -and $task -and $task.Actions[0].Execute -match '^(.*)\\python\\pythonw\.exe$') {
    $InstallDir = $Matches[1]                                            # 작업이 가리키는 설치 폴더
}
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
$ours = (Test-Path -LiteralPath (Join-Path $InstallDir 'VERSION')) -or
        (Test-Path -LiteralPath (Join-Path $InstallDir 'python\Lib\site-packages\minedocscan'))
$pyDir = Join-Path $InstallDir 'python'
$scripts = Join-Path $pyDir 'Scripts'
$defaultCfg = Join-Path (Join-Path $env:APPDATA 'minedocscan') 'minedocscan.toml'
$cfg = [Environment]::GetEnvironmentVariable('MINEDOCSCAN_CONFIG', 'User')
if (-not $cfg) { $cfg = $defaultCfg }

# 남길 데이터 폴더 (지우기 전에 — 설정을 읽는 데 그 파이썬을 쓴다)
$kept = @()
$py = Join-Path $pyDir 'python.exe'
if ($ours -and (Test-Path $py) -and (Test-Path -LiteralPath $cfg)) {
    # 큰따옴표가 없는 코드 (Windows PowerShell 5.1 은 바깥 프로그램에 넘기는 인자의 큰따옴표를 지운다). 키는 인자로
    $code = 'import sys; from minedocscan.config import load_settings; s = load_settings(sys.argv[1]); ' +
            '[print(k, getattr(s, k)) for k in sys.argv[2:] if getattr(s, k)]'
    $ErrorActionPreference = 'Continue'
    $enc = [Console]::OutputEncoding
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    try { $kept = @(& $py -c $code $cfg site archive_root work_root inbox excel_dir 2>$null) } finally { [Console]::OutputEncoding = $enc }
    $ErrorActionPreference = 'Stop'
}

# 작업
if ($task) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Say "작업 스케줄러의 $TaskName 을 지웠습니다"
}
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe' OR Name = 'minedocscan.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($pyDir + '\', [System.StringComparison]::OrdinalIgnoreCase) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# 바로 가기
$desktop = [Environment]::GetFolderPath('Desktop')
if ($desktop) {
    $lnk = Join-Path $desktop '광산 문서 스캔.url'
    if (Test-Path -LiteralPath $lnk) { Remove-Item -LiteralPath $lnk -Force }
}

# 사용자 PATH (펼치지 않은 채로 읽고 쓴다)
$key = Get-Item 'HKCU:\Environment'
$userPath = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
$parts = @($userPath -split ';' | Where-Object { $_ -ne '' })
$rest = @($parts | Where-Object { $_.TrimEnd('\') -ine $scripts.TrimEnd('\') })
if ($rest.Count -ne $parts.Count) {
    New-ItemProperty -Path 'HKCU:\Environment' -Name 'Path' -Value ($rest -join ';') -PropertyType ExpandString -Force | Out-Null
}
[Environment]::SetEnvironmentVariable('MINEDOCSCAN_CONFIG', $null, 'User')   # 지우고 바뀐 환경을 알린다

# 프로그램 (알고 있는 것만 — 그 밖의 것은 남긴다). minedocscan 의 설치 폴더가 아니면 아무것도 지우지 않는다
$names = if ($ours) { @('python', 'python.new', 'python.old', 'logs', 'VERSION') } else { @() }
if (-not $ours -and (Test-Path -LiteralPath $InstallDir)) { Write-Host "minedocscan 의 설치 폴더가 아니라 지우지 않았습니다: $InstallDir" -ForegroundColor Yellow }
foreach ($name in $names) {
    $p = Join-Path $InstallDir $name
    if (Test-Path -LiteralPath $p) {
        for ($i = 0; $i -lt 20; $i++) {
            try { Remove-Item -LiteralPath $p -Recurse -Force; break } catch { Start-Sleep -Milliseconds 500 }
        }
        if (Test-Path -LiteralPath $p) { Write-Host "지우지 못했습니다 (쓰고 있는 프로그램이 있습니다): $p" -ForegroundColor Yellow }
    }
}
if ($ours -and (Test-Path -LiteralPath $InstallDir) -and -not (Get-ChildItem -LiteralPath $InstallDir -Force | Select-Object -First 1)) {
    Remove-Item -LiteralPath $InstallDir -Force
} elseif ($ours -and (Test-Path -LiteralPath $InstallDir)) {
    Say "남은 것이 있어 폴더를 두었습니다: $InstallDir"
}

if ($RemoveConfig -and (Test-Path -LiteralPath $defaultCfg)) {
    Remove-Item -LiteralPath $defaultCfg -Force
    $dir = Split-Path -Parent $defaultCfg
    if (-not (Get-ChildItem -LiteralPath $dir -Force | Select-Object -First 1)) { Remove-Item -LiteralPath $dir -Force }
    Say "설정 파일을 지웠습니다: $defaultCfg"
}

Write-Host "minedocscan 을 지웠습니다 — 프로그램만."
Write-Host "남겨 둔 것 (지우지 않았습니다):"
if (Test-Path -LiteralPath $cfg) { Write-Host "  설정: $cfg" }
foreach ($line in $kept) { Write-Host "  $line" }
exit 0
