@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1

REM ═══════════════════════════════════════════════════════════════════
REM  Hidra Control Plane — Instalador
REM
REM  O que faz:
REM    1. Valida Python 3.13+
REM    2. Cria .venv na raiz do projeto
REM    3. Instala dependências (pyproject.toml)
REM    4. Cria atalho "Hidra Control Plane.lnk" na Área de Trabalho
REM    5. Inicia o painel (opcional)
REM ═══════════════════════════════════════════════════════════════════

title Hidra Control Plane - Instalador

REM Raiz do projeto = pai de scripts/
set "SCRIPT_DIR=%~dp0"
set "ROOT=%SCRIPT_DIR%.."
pushd "%ROOT%"
set "ROOT=%CD%"
popd

echo.
echo ============================================================
echo   Hidra Control Plane - Instalador
echo ============================================================
echo   Raiz do projeto: %ROOT%
echo.

REM ─── 1. Validar Python ─────────────────────────────────────────
echo [1/4] Verificando Python...

set "PY_EXE="
for %%p in (python3.13.exe python.exe py.exe) do (
    where %%p >nul 2>&1
    if !errorlevel! equ 0 (
        if "!PY_EXE!"=="" set "PY_EXE=%%p"
    )
)

if "%PY_EXE%"=="" (
    echo [ERRO] Python nao encontrado no PATH.
    echo        Instale Python 3.13+ em https://www.python.org/downloads/
    pause
    exit /b 1
)

REM Verificar versao >= 3.13
"%PY_EXE%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 13) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERRO] Python 3.13+ requerido. Versao encontrada:
    "%PY_EXE%" --version
    pause
    exit /b 1
)

echo        OK — %PY_EXE%
"%PY_EXE%" --version

REM ─── 2. Criar/atualizar .venv ──────────────────────────────────
echo.
echo [2/4] Criando ambiente virtual .venv...

if not exist "%ROOT%\.venv\Scripts\python.exe" (
    "%PY_EXE%" -m venv "%ROOT%\.venv"
    if errorlevel 1 (
        echo [ERRO] Falha ao criar .venv
        pause
        exit /b 1
    )
    echo        .venv criado
) else (
    echo        .venv ja existe
)

set "VENV_PY=%ROOT%\.venv\Scripts\python.exe"

REM ─── 3. Instalar dependencias ──────────────────────────────────
echo.
echo [3/4] Instalando dependencias (pode demorar 1-3 min)...

"%VENV_PY%" -m pip install --upgrade pip --quiet
"%VENV_PY%" -m pip install -e "%ROOT%" --quiet

if errorlevel 1 (
    echo [ERRO] Falha ao instalar dependencias. Rode manualmente:
    echo        %VENV_PY% -m pip install -e %ROOT%
    pause
    exit /b 1
)

echo        dependencias instaladas

REM ─── 4. Criar atalho na Area de Trabalho ───────────────────────
echo.
echo [4/4] Criando atalho na Area de Trabalho...

set "DESKTOP=%USERPROFILE%\Desktop"
set "SHORTCUT=%DESKTOP%\Hidra Control Plane.lnk"
set "TARGET=%ROOT%\scripts\iniciar_painel.vbs"

REM Usar PowerShell para criar .lnk (WshShell via cmd direto é chato)
powershell -NoProfile -Command ^
    "$WS = New-Object -ComObject WScript.Shell; ^
     $SC = $WS.CreateShortcut('%SHORTCUT%'); ^
     $SC.TargetPath = 'wscript.exe'; ^
     $SC.Arguments = '\"%TARGET%\"'; ^
     $SC.WorkingDirectory = '%ROOT%'; ^
     $SC.IconLocation = '%SystemRoot%\System32\shell32.dll,13'; ^
     $SC.Description = 'Hidra Control Plane - Orquestrador de automacoes'; ^
     $SC.Save()"

if exist "%SHORTCUT%" (
    echo        Atalho criado: %SHORTCUT%
) else (
    echo [AVISO] Nao foi possivel criar o atalho automaticamente.
    echo         Crie manualmente apontando para: %TARGET%
)

REM ─── Final ─────────────────────────────────────────────────────
echo.
echo ============================================================
echo   INSTALACAO CONCLUIDA
echo ============================================================
echo.
echo   Para iniciar o painel:
echo     - Duplo-clique em "Hidra Control Plane" na Area de Trabalho
echo     - Ou execute: %TARGET%
echo.
echo   Dashboard: http://localhost:9000
echo.

choice /C SN /M "Deseja iniciar o painel agora"
if errorlevel 2 goto :end
if errorlevel 1 (
    start "" wscript.exe "%TARGET%"
)

:end
endlocal
exit /b 0
