$startup = [Environment]::GetFolderPath('Startup')
$link = Join-Path $startup "OCS AI Gateway.lnk"
if (Test-Path $link) {
  Remove-Item $link -Force
  Write-Output "disabled: True"
} else {
  Write-Output "disabled: already off"
}
