@echo off
rem Programin masaustu ve Baslat menusu kisayollarini olusturur. Bu dosya exe ile ayni klasorde durmalidir.
set "KLASOR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$exe = Get-ChildItem -LiteralPath $env:KLASOR -Filter *.exe | Select-Object -First 1;" ^
  "if (-not $exe) { Write-Host 'Program bulunamadi.'; exit 1 }" ^
  "$sh = New-Object -ComObject WScript.Shell;" ^
  "foreach ($yer in [Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs')) {" ^
  "  $lnk = $sh.CreateShortcut((Join-Path $yer ($exe.BaseName + '.lnk')));" ^
  "  $lnk.TargetPath = $exe.FullName; $lnk.WorkingDirectory = $exe.DirectoryName;" ^
  "  $lnk.IconLocation = $exe.FullName + ',0'; $lnk.Save() }" ^
  "Write-Host ('Kisayol olusturuldu: ' + $exe.BaseName)"
pause
