@echo off
rem Restart Tighnari-SeisLOC after a crash. Set TIGHNARI_CONDA_ENV / TIGHNARI_CONDA_ACTIVATE to override.
cd /d "%~dp0"
if not defined TIGHNARI_CONDA_ENV set "TIGHNARI_CONDA_ENV=tighnari"
if not defined TIGHNARI_CONDA_ACTIVATE set "TIGHNARI_CONDA_ACTIVATE=C:\ProgramData\anaconda3\Scripts\activate.bat"
call "%TIGHNARI_CONDA_ACTIVATE%" "%TIGHNARI_CONDA_ENV%"
:start
python start.py
timeout /t 5 /nobreak
goto start
