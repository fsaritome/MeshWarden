# build-wifi-companion.ps1 - part of MeshWarden (https://github.com/fsaritome/MeshWarden)
# Copyright (c) 2026 Ferit Sari Tomé - MIT License
# Run from the root of a MeshCore clone. Asks for WiFi SSID/password, builds + flashes the
# Heltec V4 WiFi companion firmware, then restores the ini and deletes temp files with the password.

# Builds and flashes the MeshCore WiFi companion for the Heltec V4.
#   .\build-wifi-companion.ps1            normal output, live
#   .\build-wifi-companion.ps1 -Full      full PlatformIO output (every compiler command, -v)
#   .\build-wifi-companion.ps1 -NoUpload  build only
# Your password is typed here, never printed (masked as ******** if it appears in output),
# and removed from platformio.ini and the build temp files afterwards.
param([switch]$Full, [switch]$NoUpload)
Set-Location $PSScriptRoot
$ini = "variants\heltec_v4\platformio.ini"
$env_ = "heltec_v4_companion_radio_wifi"
$ssid = Read-Host "WiFi name (SSID)"
$sec  = Read-Host "WiFi password" -AsSecureString
$wifiPw = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
if ($ssid -match "[`"'\\]" -or $wifiPw -match "[`"'\\]") { Write-Host "SSID/password contains quotes or backslashes - not supported by the build." -ForegroundColor Red; exit 1 }
Write-Host "SSID: $ssid  (password length $($wifiPw.Length))"
git checkout -- $ini
$orig = Get-Content $ini -Raw
try {
  $new = $orig.Replace("-D WIFI_SSID='`"myssid`"'", "-D WIFI_SSID='`"$ssid`"'").Replace("-D WIFI_PWD='`"mypwd`"'", "-D WIFI_PWD='`"$wifiPw`"'")
  if ($new -eq $orig) { Write-Host "Placeholder lines not found in $ini" -ForegroundColor Red; exit 1 }
  Set-Content $ini $new -NoNewline
  $args_ = @('-m','platformio','run','-e',$env_)
  if (-not $NoUpload) { $args_ += @('-t','upload') }
  if ($Full) { Write-Host '-Full disabled: PlatformIO -v crashes on this platform when building firmware.bin (SCons print bug). Using normal output.' -ForegroundColor Yellow }
  Write-Host "Running: python $($args_ -join ' ')" -ForegroundColor Cyan
  $ErrorActionPreference = 'Continue'
  & cmd /c "python $($args_ -join ' ') 2>&1" | ForEach-Object { $l = "$_"; if ($wifiPw -and $l.Contains($wifiPw)) { $l = $l.Replace($wifiPw,'********') }; Write-Host $l }
  if ($LASTEXITCODE -eq 0) { Write-Host "SUCCESS" -ForegroundColor Green } else { Write-Host "FAILED (exit $LASTEXITCODE)" -ForegroundColor Red }
} finally {
  git checkout -- $ini
  Remove-Item ".pio\build\$env_\tmp*.tmp" -ErrorAction SilentlyContinue
  $wifiPw = $null
}

