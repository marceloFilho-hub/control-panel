@echo off
chcp 65001 >nul 2>&1
title Hidra Control Plane - Desinstalador

echo.
echo ============================================================
echo   Hidra Control Plane - Desinstalador
echo ============================================================
echo.
echo Este script NAO remove o codigo do projeto nem os logs.
echo Remove apenas:
echo   - Atalho "Hidra Control Plane" da Area de Trabalho
echo   - Processos rodando (Control Panel + subprocessos)
echo.

choice /C SN /M "Continuar"
if errorlevel 2 exit /b 0

REM Matar processos na porta 9000
echo.
echo Parando processos...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":9000.*LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)

REM Remover atalho
set "SHORTCUT=%USERPROFILE%\Desktop\Hidra Control Plane.lnk"
if exist "%SHORTCUT%" (
    del "%SHORTCUT%"
    echo Atalho removido: %SHORTCUT%
) else (
    echo Atalho nao encontrado ^(ja removido^).
)

echo.
echo Desinstalacao concluida.
echo Para reinstalar: scripts\install.bat
echo.
pause
exit /b 0
