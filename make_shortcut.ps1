$project = Join-Path $env:USERPROFILE "Documents\Qoder\2026-11-08\62711a50\ocs-ai-gateway"
$target = Join-Path $project "start-gateway.bat"
$desktop = Join-Path $env:USERPROFILE "Desktop"
$link = Join-Path $desktop "OCS AI Gateway.lnk"

$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($link)
$sc.TargetPath = $target
$sc.WorkingDirectory = $project
$sc.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
$sc.Description = "Start OCS AI Gateway (local question bank + multi-model answering)"
$sc.WindowStyle = 1
$sc.Save()

Write-Output ("created: " + (Test-Path $link))
Write-Output ("link: " + $link)
Write-Output ("target exists: " + (Test-Path $target))
