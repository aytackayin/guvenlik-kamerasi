@echo off
rem Paketi "dist\Güvenlik Kamerası" klasörüne derler. Gerekenler: Python, PyInstaller, Pillow, ffmpeg (PATH'te).
chcp 65001 >nul
cd /d "%~dp0"
python simge_olustur.py || goto :hata
python -m PyInstaller --noconfirm --clean --distpath "%~dp0..\dist" --workpath "%~dp0..\build" guvenlik.spec || goto :hata
copy /y "%~dp0kur.bat" "%~dp0..\dist\Güvenlik Kamerası\Masaüstüne kısayol ekle.bat" >nul
echo.
echo Hazir: %~dp0..\dist\Güvenlik Kamerası
exit /b 0
:hata
echo Derleme basarisiz.
exit /b 1
