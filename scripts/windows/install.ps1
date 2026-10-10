<#
.SYNOPSIS
  minedocscan 설치 (tasks/0009 4.5 나) — 관리자 권한 없이, 지금 사용자로, 망에 닿지 않고.

.DESCRIPTION
  묶음 폴더(이 스크립트가 있는 곳)에서:
   1. SHA256SUMS.txt 로 묶음을 확인한다. 인터넷에서 받은 zip 의 차단 표시(Mark of the Web)가 남아 있으면 푼다 (Unblock-File).
   2. 앱 전용 파이썬: embeddable 패키지를 InstallDir\python 에 푼다 (레지스트리·시작 메뉴를 건드리지 않는다). 다시 설치하면(올리기)
      옆에 python.new 를 만들어 다 된 뒤 바꿔 끼운다 — 실패하면 옛것이 그대로다.
   3. 그 파이썬에 pip(묶음의 바퀴)과 minedocscan[postgres](wheels\)를 --no-index 로 — 망에 닿지 않는다.
   4. 런타임 확인: cv2·numpy·pypdfium2·psycopg·openpyxl 을 읽어 들인다.
   5. 설정: %APPDATA%\minedocscan\minedocscan.toml 이 없으면 매개변수로 쓴다 (있으면 건드리지 않는다). -Config 를 주면 그 파일을 쓴다.
      사용자 환경 변수 MINEDOCSCAN_CONFIG, 사용자 PATH 에 InstallDir\python\Scripts.
   6. minedocscan info.
   7. 작업 스케줄러 minedocscan-serve: 로그온할 때, pythonw(창 없이), 실패하면 1분 뒤 다시(세 번), 시간 제한 없음, 배터리에서도.
   8. 바탕 화면에 바로 가기 "광산 문서 스캔" → http://127.0.0.1:8765/
   9. 요약.
  통합 DB 의 URL 은 받지 않는다 — 비밀번호는 %APPDATA%\postgresql\pgpass.conf (설명서).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install.ps1 -Site D:\minedocscan\site -Archive D:\minedocscan\archive `
      -Work D:\minedocscan\work -Inbox D:\Scans -Reviewer jp
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'minedocscan'),
    [string]$Site,
    [string]$Archive,
    [string]$Work,
    [string]$Inbox,
    [string]$ExcelDir,
    [string]$Config,
    [string]$Reviewer,
    [switch]$NoTask,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$TaskName = 'minedocscan-serve'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Utf8 = New-Object System.Text.UTF8Encoding $false
$started = Get-Date

function Say([string]$text) { if (-not $Quiet) { Write-Host $text } }
function Fail([string]$text) { Write-Host "설치를 멈춥니다: $text" -ForegroundColor Red; exit 1 }

function Test-Absolute([string]$p) { return [System.IO.Path]::IsPathRooted($p) -and ($p -match '^[A-Za-z]:\\|^\\\\') }

function Get-UserPath {
    # 사용자 PATH 를 펼치지 않은 채로 (%USERPROFILE%… 가 그대로 남게 — SetEnvironmentVariable 은 REG_SZ 로 바꿔 버린다)
    $key = Get-Item 'HKCU:\Environment'
    return [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
}

function Set-UserPath([string]$value) {
    New-ItemProperty -Path 'HKCU:\Environment' -Name 'Path' -Value $value -PropertyType ExpandString -Force | Out-Null
}

function Toml-String([string]$s) {
    # TOML 의 문자 그대로인 글자열('…'). 작은따옴표가 있으면 기본 글자열로 (\ 와 " 를 escape)
    if ($s -notmatch "'") { return "'" + $s + "'" }
    return '"' + ($s -replace '\\', '\\' -replace '"', '\"') + '"'
}

function Invoke-Py([string[]]$arguments) {
    # 파이썬이 표준 오류에 쓴 줄(pip 의 경고 …)이 Stop 아래에서 멈춤이 되지 않게 이 함수 안에서만 Continue.
    # 파이썬은 파이프로 UTF-8 을 쓴다 (cli.utf8_streams) — 받는 쪽도 UTF-8 로 읽고 돌려 둔다 (안 그러면 한글이 깨진다)
    $ErrorActionPreference = 'Continue'
    $enc = [Console]::OutputEncoding
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    try {
        $out = & $script:Py @arguments 2>&1
        $code = $LASTEXITCODE
    } finally {
        [Console]::OutputEncoding = $enc
    }
    return @{ Code = $code; Out = @($out | ForEach-Object { "$_" }) }
}

function Rename-Retry([string]$path, [string]$name) {
    # 백신·색인기가 잠깐 쥐고 있을 수 있다 — 10초까지 다시 해 본다
    for ($i = 0; $i -lt 20; $i++) {
        try { Rename-Item -LiteralPath $path -NewName $name; return $true } catch { Start-Sleep -Milliseconds 500 }
    }
    return $false
}

function Test-Ours([string]$dir) {
    # 이 설치 폴더가 minedocscan 의 것인가 (VERSION 이나 앱 전용 파이썬 안의 minedocscan) — 남의 python\ 을 지우지 않는다
    return (Test-Path -LiteralPath (Join-Path $dir 'VERSION')) -or
           (Test-Path -LiteralPath (Join-Path $dir 'python\Lib\site-packages\minedocscan'))
}

# ── 1. 묶음 확인 ─────────────────────────────────────────────────────────────
$sums = Join-Path $Here 'SHA256SUMS.txt'
if (-not (Test-Path $sums)) { Fail "SHA256SUMS.txt 가 없습니다 — 묶음 zip 을 통째로 풀었는지 보십시오" }
$version = (Get-Content (Join-Path $Here 'VERSION') -Raw).Trim()
$bad = @()
$listed = @{}
foreach ($line in Get-Content $sums) {
    if ($line -notmatch '^([0-9a-f]{64})  (.+)$') { continue }
    $listed[$Matches[2]] = $true
    $file = Join-Path $Here ($Matches[2] -replace '/', '\')
    if (-not (Test-Path -LiteralPath $file)) { $bad += $Matches[2]; continue }
    $got = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLower()
    if ($got -ne $Matches[1]) { $bad += $Matches[2] }
}
# 설치하는 것(wheels\, 파이썬, pip, 스크립트)은 목록에 있어야 한다 — 비었거나 잘린 목록, 끼워 넣은 바퀴를 받지 않는다
$must = @(Get-ChildItem -LiteralPath (Join-Path $Here 'wheels') -File -ErrorAction SilentlyContinue | ForEach-Object { "wheels/$($_.Name)" })
$must += @(Get-ChildItem -LiteralPath $Here -File | Where-Object { $_.Name -match '\.(zip|whl|ps1)$' } | ForEach-Object { $_.Name })
foreach ($m in $must) { if (-not $listed.ContainsKey($m)) { $bad += $m } }
if ($listed.Count -eq 0) { $bad += 'SHA256SUMS.txt' }
if ($bad.Count -gt 0) { Fail ("묶음이 손상되었습니다 (해시가 다른 파일 $($bad.Count)개: " + (($bad | Select-Object -First 3) -join ', ') + ") — 다시 받으십시오") }
$blocked = @(Get-ChildItem -LiteralPath $Here -Recurse -File | Where-Object { Get-Item -LiteralPath $_.FullName -Stream Zone.Identifier -ErrorAction SilentlyContinue })
if ($blocked.Count -gt 0) {
    $blocked | Unblock-File
    Say "인터넷에서 받은 표시(차단)를 풀었습니다: 파일 $($blocked.Count)개"
}
Say "묶음 minedocscan $version — 확인했습니다"

# ── 설정을 먼저 정한다 (파이썬을 바꾸기 전에 멈출 수 있는 것은 다 멈춘다) ──────────────
$cfgDir = Join-Path $env:APPDATA 'minedocscan'
$defaultCfg = Join-Path $cfgDir 'minedocscan.toml'
$writeCfg = $false
$knownCfg = [Environment]::GetEnvironmentVariable('MINEDOCSCAN_CONFIG', 'User')
if ($Config) {
    if (-not (Test-Path -LiteralPath $Config)) { Fail "설정 파일이 없습니다: -Config $Config" }
    $cfg = (Resolve-Path -LiteralPath $Config).ProviderPath            # UNC 도 파이썬이 여는 모양으로 (Microsoft.PowerShell.Core\FileSystem:: 없이)
} elseif (Test-Path -LiteralPath $defaultCfg) {
    $cfg = $defaultCfg                                                  # 있으면 건드리지 않는다
} elseif ($knownCfg -and (Test-Path -LiteralPath $knownCfg)) {
    $cfg = $knownCfg                                                    # 올리기: 처음에 -Config 로 준 설정을 그대로
} else {
    $cfg = $defaultCfg
    $writeCfg = $true
    foreach ($pair in @(@('Site', $Site), @('Archive', $Archive), @('Work', $Work))) {
        if (-not $pair[1]) { Fail "처음 설치에는 -$($pair[0]) 가 있어야 합니다 (또는 기존 설정 파일 -Config)" }
    }
    foreach ($pair in @(@('Site', $Site), @('Archive', $Archive), @('Work', $Work), @('Inbox', $Inbox), @('ExcelDir', $ExcelDir))) {
        if ($pair[1] -and -not (Test-Absolute $pair[1])) { Fail "-$($pair[0]) 는 절대 경로로 적습니다 (C:\… 또는 \\서버\…): $($pair[1])" }
    }
}
if (-not $NoTask) {
    if (-not $Reviewer) { Fail "작업 스케줄러에 serve 를 등록하려면 -Reviewer (검수자 ID — 짧은 영문)가 있어야 합니다 (아니면 -NoTask)" }
    if ($Reviewer -notmatch '^[A-Za-z0-9_.-]{1,32}$') { Fail "-Reviewer 는 영문·숫자·_.- 32자 안: $Reviewer" }
}

# ── 2. 앱 전용 파이썬 (옆에 만들어 바꿔 끼운다) ─────────────────────────────────────
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$InstallDir = (Resolve-Path -LiteralPath $InstallDir).ProviderPath
$pyDir = Join-Path $InstallDir 'python'
$newDir = Join-Path $InstallDir 'python.new'
$oldDir = Join-Path $InstallDir 'python.old'
if ((Test-Path $pyDir) -and -not (Test-Ours $InstallDir)) { Fail "$InstallDir 에 다른 프로그램의 python 폴더가 있습니다 — 빈 폴더를 -InstallDir 로 주십시오" }
if (-not (Test-Path $pyDir) -and (Test-Path (Join-Path $oldDir 'python.exe'))) {
    Rename-Retry $oldDir 'python' | Out-Null                            # 지난번 바꿔 끼우기가 끊겼다 — 옛 판을 되돌려 놓는다
}
$upgrade = Test-Path (Join-Path $pyDir 'python.exe')
$oldSchema = $null
if ($upgrade) {
    $script:Py = Join-Path $pyDir 'python.exe'
    $r = Invoke-Py @('-c', 'from minedocscan.store.db import SCHEMA_VERSION; print(SCHEMA_VERSION)')
    if ($r.Code -eq 0) { $oldSchema = ($r.Out | Select-Object -Last 1).Trim() }
}
foreach ($d in @($newDir, $oldDir)) { if (Test-Path $d) { Remove-Item -Recurse -Force $d } }
$embed = Get-ChildItem -LiteralPath $Here -Filter 'python-*-embed-amd64.zip' | Select-Object -First 1
$pipWhl = Get-ChildItem -LiteralPath $Here -Filter 'pip-*.whl' | Select-Object -First 1
if (-not $embed -or -not $pipWhl) { Fail "묶음에 파이썬(embeddable zip)이나 pip 바퀴가 없습니다" }
Say "앱 전용 파이썬을 풉니다 ($($embed.Name))"
Expand-Archive -LiteralPath $embed.FullName -DestinationPath $newDir -Force
$pth = Get-ChildItem -LiteralPath $newDir -Filter 'python*._pth' | Select-Object -First 1
if (-not $pth) { Fail "embeddable 패키지에 ._pth 파일이 없습니다" }
$lines = Get-Content -LiteralPath $pth.FullName | ForEach-Object { if ($_ -match '^\s*#\s*import site\s*$') { 'import site' } else { $_ } }
[System.IO.File]::WriteAllLines($pth.FullName, [string[]]$lines, $Utf8)   # site-packages 와 pip 의 Scripts 를 쓰려면 import site
$script:Py = Join-Path $newDir 'python.exe'

$env:PIP_NO_INDEX = '1'                                                  # 망에 닿지 않는다
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
$env:PYTHONUTF8 = '1'
try {
    # pip: 바퀴를 site-packages 에 풀고(순수 파이썬) 그 pip 으로 자기를 다시 설치한다 (RECORD·Scripts 가 생긴다).
    # embeddable 은 ._pth 가 sys.path 를 정하므로 바퀴 안에서 바로 돌리는 길(python pip.whl/pip)에 기대지 않는다
    $sp = Join-Path $newDir 'Lib\site-packages'
    New-Item -ItemType Directory -Force -Path $sp | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($pipWhl.FullName, $sp)
    $r = Invoke-Py @('-m', 'pip', 'install', '--isolated', '--no-index', '--no-deps', '--force-reinstall', '--no-warn-script-location',
                     '--quiet', $pipWhl.FullName)
    if ($r.Code -ne 0) { throw ("pip 을 놓지 못했습니다: " + ($r.Out | Select-Object -Last 1)) }
    Say "minedocscan 과 의존성을 설치합니다 (wheels\, 망 없이)"
    $r = Invoke-Py @('-m', 'pip', 'install', '--isolated', '--no-index', '--find-links', (Join-Path $Here 'wheels'),
                     '--no-warn-script-location', '--quiet', 'minedocscan[postgres]')
    if ($r.Code -ne 0) { throw ("설치하지 못했습니다: " + (($r.Out | Select-Object -Last 3) -join ' / ')) }
    # ── 4. 런타임 확인 ──
    $r = Invoke-Py @('-c', 'import cv2, numpy, pypdfium2, psycopg, openpyxl; print(cv2.__version__)')
    if ($r.Code -ne 0) {
        $why = ($r.Out | Select-Object -Last 1)
        throw ("런타임 확인에 실패했습니다 ($why). 'DLL load failed' 이면 이 PC 에 Visual C++ 재배포 패키지(x64)가 필요합니다 " +
               "— 그리고 N·KN 판 윈도우라면 미디어 기능 팩")
    }
} catch {
    if (Test-Path $newDir) { Remove-Item -Recurse -Force $newDir }
    Fail ("$($_.Exception.Message)" + $(if ($upgrade) { " — 옛 판은 그대로입니다" } else { "" }))
}

# 바꿔 끼운다: 작업을 멈추고(올리기), python → python.old, python.new → python
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe' OR Name = 'minedocscan.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($pyDir + '\', [System.StringComparison]::OrdinalIgnoreCase) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
if ($upgrade -and -not (Rename-Retry $pyDir 'python.old')) {
    Remove-Item -Recurse -Force $newDir
    Fail "옛 파이썬을 바꾸지 못했습니다 (쓰고 있는 프로그램이 있습니다) — 옛 판은 그대로입니다"
}
if (-not (Rename-Retry $newDir 'python')) {
    if ($upgrade) { Rename-Retry $oldDir 'python' | Out-Null }           # 되돌린다
    Remove-Item -Recurse -Force $newDir -ErrorAction SilentlyContinue
    Fail ("새 파이썬을 제자리에 두지 못했습니다" + $(if ($upgrade) { " — 옛 판으로 되돌렸습니다" } else { "" }))
}
if (Test-Path $oldDir) { Remove-Item -Recurse -Force $oldDir -ErrorAction SilentlyContinue }
$script:Py = Join-Path $pyDir 'python.exe'
# 명령 실행 파일(Scripts\minedocscan.exe·pip.exe)에는 pip 을 돌린 파이썬의 경로(python.new)가 박혀 있다 — 제자리의 파이썬으로 다시 만든다
$r = Invoke-Py @('-m', 'pip', 'install', '--isolated', '--no-index', '--no-deps', '--force-reinstall', '--no-warn-script-location',
                 '--quiet', '--find-links', (Join-Path $Here 'wheels'), 'minedocscan', $pipWhl.FullName)
if ($r.Code -ne 0) { Fail ("명령 실행 파일을 다시 만들지 못했습니다: " + ($r.Out | Select-Object -Last 1)) }
$pyw = Join-Path $pyDir 'pythonw.exe'
$scripts = Join-Path $pyDir 'Scripts'
$newSchema = ((Invoke-Py @('-c', 'from minedocscan.store.db import SCHEMA_VERSION; print(SCHEMA_VERSION)')).Out | Select-Object -Last 1).Trim()

# ── 5. 설정·환경 변수·PATH ───────────────────────────────────────────────────
if ($writeCfg) {
    New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null
    $toml = @("# minedocscan 설정 — install.ps1 이 썼다 ($(Get-Date -Format 'yyyy-MM-dd')). 보기: 묶음의 minedocscan.example.toml",
              "# 통합 DB 의 주소는 여기에 적지 않는다 — 사용자 환경 변수 MINEDOCSCAN_PUBLISH_URL, 비밀번호는 %APPDATA%\postgresql\pgpass.conf",
              "[paths]",
              "site = $(Toml-String $Site)",
              "archive_root = $(Toml-String $Archive)",
              "work_root = $(Toml-String $Work)")
    if ($Inbox) { $toml += "inbox = $(Toml-String $Inbox)" }
    if ($ExcelDir) { $toml += @("", "[export]", "excel_dir = $(Toml-String $ExcelDir)") }
    [System.IO.File]::WriteAllText($cfg, (($toml -join "`r`n") + "`r`n"), $Utf8)   # BOM 없이 (TOML 은 BOM 을 받지 않는다)
    Say "설정 파일을 썼습니다: $cfg"
} else {
    Say "설정 파일은 그대로 둡니다: $cfg"
}
$userPath = Get-UserPath
$parts = @($userPath -split ';' | Where-Object { $_ -ne '' })
if (-not ($parts | Where-Object { $_.TrimEnd('\') -ieq $scripts.TrimEnd('\') })) {
    Set-UserPath ((@($parts) + $scripts) -join ';')
}
[Environment]::SetEnvironmentVariable('MINEDOCSCAN_CONFIG', $cfg, 'User')   # 바꾼 환경을 알린다 (새 창이 PATH 도 다시 읽는다)
$env:MINEDOCSCAN_CONFIG = $cfg
if (-not (($env:Path -split ';') -contains $scripts)) { $env:Path = "$env:Path;$scripts" }

# ── 6. info ──────────────────────────────────────────────────────────────────
$r = Invoke-Py @('-m', 'minedocscan.cli', 'info', '--config', $cfg)
if (-not $Quiet) { $r.Out | ForEach-Object { Write-Host "  $_" } }
if ($r.Code -ne 0) { Write-Host "minedocscan info 가 실패했습니다 — 설정의 경로를 보십시오 (설치는 끝났습니다)" -ForegroundColor Yellow }

# ── 7. 작업 스케줄러 ──────────────────────────────────────────────────────────
$logs = Join-Path $InstallDir 'logs'
if (-not $NoTask) {
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $arg = "-m minedocscan.cli serve --config `"$cfg`" --reviewer $Reviewer --log-dir `"$logs`""
    $action = New-ScheduledTaskAction -Execute $pyw -Argument $arg -WorkingDirectory $InstallDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
        -Description 'minedocscan 운영 화면과 접수 폴더 감시 (127.0.0.1:8765)' -Force | Out-Null
    try { Start-ScheduledTask -TaskName $TaskName } catch { Say "작업을 지금 켜지 못했습니다 — 다음 로그온에 켜집니다" }
    Say "작업 스케줄러에 $TaskName 을 등록했습니다 (로그온할 때, 창 없이)"
} elseif ($task) {
    try { Start-ScheduledTask -TaskName $TaskName } catch { }           # -NoTask 의 올리기: 멈췄던 기존 작업을 다시 켠다
}

# ── 8. 바로 가기 ─────────────────────────────────────────────────────────────
$desktop = [Environment]::GetFolderPath('Desktop')
if ($desktop) {
    $lnk = Join-Path $desktop '광산 문서 스캔.url'
    [System.IO.File]::WriteAllText($lnk, "[InternetShortcut]`r`nURL=http://127.0.0.1:8765/`r`n", $Utf8)
}
Set-Content -LiteralPath (Join-Path $InstallDir 'VERSION') -Value $version -Encoding Ascii

# ── 9. 요약 ──────────────────────────────────────────────────────────────────
$secs = [int]((Get-Date) - $started).TotalSeconds
Write-Host ""
Write-Host "minedocscan $version 을 설치했습니다 ($secs 초)$(if ($upgrade) { ' — 올렸습니다' })"
Write-Host "  프로그램: $InstallDir (앱 전용 파이썬 python\, 로그 logs\)"
Write-Host "  설정: $cfg"
if (-not $NoTask) { Write-Host "  화면: http://127.0.0.1:8765/ (바탕 화면의 '광산 문서 스캔')" }
if ($upgrade -and $oldSchema -and $newSchema -and ($oldSchema -ne $newSchema)) {
    Write-Host "  작업 DB 의 스키마가 바뀌었습니다 ($oldSchema → $newSchema): 새 창에서 minedocscan run --fresh 로 다시 만드십시오 (검수·결정 기록은 그대로)" -ForegroundColor Yellow
}
Write-Host "다음: 새 PowerShell 창에서 'minedocscan selftest' — 그리고 설명서(묶음의 manual.html)의 3장 '처음 설정'"
exit 0
