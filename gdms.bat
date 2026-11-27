@echo off
setlocal
chcp 65001 >nul
title Tighnari GDMS
cd /d "%~dp0"
if errorlevel 1 goto project_error

rem Overrides accept a conda env name or full path.
if not defined TIGHNARI_CONDA_ENV (
    if exist "%USERPROFILE%\.conda\envs\tighnari\python.exe" (
        set "TIGHNARI_CONDA_ENV=%USERPROFILE%\.conda\envs\tighnari"
    ) else (
        set "TIGHNARI_CONDA_ENV=tighnari"
    )
)
if not defined TIGHNARI_CONDA_ACTIVATE set "TIGHNARI_CONDA_ACTIVATE=C:\ProgramData\anaconda3\Scripts\activate.bat"
if not exist "%TIGHNARI_CONDA_ACTIVATE%" (
    echo Cannot find the conda activation script.
    echo Set TIGHNARI_CONDA_ACTIVATE to the full path of activate.bat.
    goto failed
)
call "%TIGHNARI_CONDA_ACTIVATE%" "%TIGHNARI_CONDA_ENV%"
if errorlevel 1 (
    echo Cannot activate conda environment: %TIGHNARI_CONDA_ENV%
    goto failed
)
rem Conda activation can restore the system code page.
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
python -c "import sys; print('Python: ' + sys.executable)"
if errorlevel 1 goto failed

if not exist "scripts\gdms_messages.py" (
    echo Missing scripts\gdms_messages.py. Copy the complete GDMS update first.
    goto failed
)

:menu
echo.
echo ===== Tighnari GDMS =====
python scripts\gdms_messages.py menu
if errorlevel 1 goto failed
python scripts\gdms_messages.py prompt
choice /c 123450 /n /m "[1/2/3/4/5/0]: "
if errorlevel 6 exit /b 0
if errorlevel 5 goto start_service
if errorlevel 4 goto install_edge
if errorlevel 3 goto status
if errorlevel 2 goto resume
if errorlevel 1 goto login
goto menu

:check_version
if not exist "scripts\gdms_control.py" (
    python scripts\gdms_messages.py missing_control
    exit /b 1
)
python scripts\gdms_control.py --help 2>nul | findstr /l /c:"{resume,login,status}" >nul
if errorlevel 1 (
    python scripts\gdms_messages.py old_control
    python scripts\gdms_messages.py sync_required
    exit /b 1
)
exit /b 0

:check_service
python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3).close()" >nul 2>&1
exit /b %errorlevel%

:login
call :check_version
if errorlevel 1 goto action_failed
call :check_service
if errorlevel 1 goto service_missing
python scripts\gdms_messages.py same_user
python scripts\gdms_messages.py login_instructions
python scripts\gdms_control.py login --timeout 600
if errorlevel 1 goto action_failed
python scripts\gdms_messages.py login_verified
goto done

:resume
call :check_version
if errorlevel 1 goto action_failed
call :check_service
if errorlevel 1 goto service_missing
python scripts\gdms_control.py resume
if errorlevel 1 goto action_failed
goto done

:status
call :check_version
if errorlevel 1 goto action_failed
python scripts\gdms_control.py status
if errorlevel 1 goto action_failed
call :check_service
if errorlevel 1 python scripts\gdms_messages.py service_unreachable_status
goto done

:install_edge
python scripts\gdms_messages.py install_edge
python -m pip install -e ".[gdms-edge]"
if errorlevel 1 goto action_failed
python scripts\gdms_messages.py edge_installed
goto done

:start_service
call :check_service
if not errorlevel 1 (
    python scripts\gdms_messages.py service_running
    goto done
)
if not exist "start.py" goto project_error
rem The child inherits this already activated conda environment and project cwd.
start "Tighnari service" "%ComSpec%" /k python start.py
python scripts\gdms_messages.py service_started
goto done

:service_missing
python scripts\gdms_messages.py service_missing
goto done

:action_failed
python scripts\gdms_messages.py action_failed
:done
echo.
pause
goto menu

:project_error
echo Cannot open project. Place gdms.bat next to start.py.
:failed
pause
exit /b 1
