$project = Join-Path $env:USERPROFILE "Documents\Qoder\2026-11-08\62711a50\ocs-ai-gateway"
$target = Join-Path $project "start-gateway.bat"
$startup = [Environment]::GetFolderPath('Startup')
$link = Join-Path $startup "OCS AI Gateway.lnk"

$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($link)
$sc.TargetPath = $target
$sc.Arguments = "--silent"
$sc.WorkingDirectory = $project
$sc.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
$sc.Description = "Auto-start OCS AI Gateway (silent)"
$sc.WindowStyle = 7
$sc.Save()

Write-Output ("startup folder: " + $startup)
Write-Output ("link: " + $link)
Write-Output ("enabled: " + (Test-Path $link))
