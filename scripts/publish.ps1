# 一键发布：跑全量测试 -> 有变更才提交 -> 推送 origin/main（失败自动重试）
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\publish.ps1 -Message "说明这次改了什么"
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\publish.ps1 -Message "只改文档" -SkipTests
param(
    [string]$Message = "",
    [switch]$SkipTests,
    [int]$Retries = 3
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 限制线程，避免本机 BLAS/OpenMP 线程池吃光内存
$env:PYTHONIOENCODING = "utf-8"
$env:OMP_NUM_THREADS = "1"
$env:OPENBLAS_NUM_THREADS = "1"
$env:MKL_NUM_THREADS = "1"
$env:NUMEXPR_NUM_THREADS = "1"

if (-not $SkipTests) {
    Write-Host "[1/3] 运行全量测试（约 90 秒）..." -ForegroundColor Cyan
    $log = Join-Path $env:TEMP "ocs-tests.log"
    $ErrorActionPreference = "Continue"
    & .\.venv\Scripts\python.exe -m unittest discover -s tests *> $log
    $code = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    Get-Content $log -Tail 6
    if ($code -ne 0) {
        Write-Host "测试未通过，已中止：没有提交，也没有推送。" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "[1/3] 已跳过测试（-SkipTests）" -ForegroundColor Yellow
}

git add -A
git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Host "没有需要提交的变更，收工。" -ForegroundColor Green
    exit 0
}

if (-not $Message) { $Message = "更新：$(Get-Date -Format 'yyyy-MM-dd HH:mm')" }
$msgFile = Join-Path $env:TEMP "ocs-commit-msg.txt"
$body = "$Message`n`nCo-Authored-By: Claude Code <noreply@anthropic.com>`n"
[System.IO.File]::WriteAllText($msgFile, $body, [System.Text.UTF8Encoding]::new($false))

Write-Host "[2/3] 提交..." -ForegroundColor Cyan
git commit -F $msgFile
if ($LASTEXITCODE -ne 0) { Write-Host "提交失败，未推送。" -ForegroundColor Red; exit 1 }

Write-Host "[3/3] 推送 origin/main..." -ForegroundColor Cyan
for ($i = 1; $i -le $Retries; $i++) {
    git -c http.version=HTTP/1.1 -c http.postBuffer=524288000 push origin main
    if ($LASTEXITCODE -eq 0) {
        Write-Host "已推送：$(git log --oneline -1)" -ForegroundColor Green
        exit 0
    }
    Write-Host "第 $i 次推送失败，$($i * 5) 秒后重试..." -ForegroundColor Yellow
    Start-Sleep -Seconds ($i * 5)
}

Write-Host "推送失败（已重试 $Retries 次）。提交已在本地，网络恢复后手动执行：git push origin main" -ForegroundColor Red
exit 2
