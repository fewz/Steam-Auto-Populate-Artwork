@echo off
rem Double-click to fill in artwork for your non-Steam games.
cd /d "%~dp0"
where py >nul 2>nul && (py -3 steam_artwork.py %*) || (python steam_artwork.py %*)
echo.
pause
