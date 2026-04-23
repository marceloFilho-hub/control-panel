' ═══════════════════════════════════════════════════════════════
'  Monitor de Contribuintes — Launcher VISÍVEL para Control Panel
'
'  Este VBS mora na pasta apps_executaveis do Control Panel, por isso
'  usa CAMINHO ABSOLUTO do projeto cadastro_contribuinte (ao invés de
'  tentar descobrir via pai-pai do próprio arquivo).
'
'  Lança pythonw.exe monitor_ui.py com janela VISÍVEL
'  (oShell.Run strCmd, 1, False — SW_SHOWNORMAL).
' ═══════════════════════════════════════════════════════════════

Option Explicit

Dim oShell, oFSO
Dim strBase, strPythonW, strScript, strCmd

Set oShell = CreateObject("WScript.Shell")
Set oFSO   = CreateObject("Scripting.FileSystemObject")

' ── Caminho ABSOLUTO do projeto real (independente de onde este VBS está) ──
strBase    = "C:\Users\marcelo.lsantos_bhub\Documents\automacoes\cadastro_contribuinte"
strPythonW = strBase & "\.venv\Scripts\pythonw.exe"
strScript  = strBase & "\src\front\monitor_ui.py"

' ── Validações ──────────────────────────────────────────────────────
If Not oFSO.FolderExists(strBase) Then
    WScript.Echo "[ERRO] Pasta do projeto nao existe: " & strBase
    WScript.Quit 2
End If

If Not oFSO.FileExists(strPythonW) Then
    WScript.Echo "[ERRO] pythonw.exe nao encontrado: " & strPythonW
    WScript.Echo "Execute o instalar.bat do projeto cadastro_contribuinte."
    WScript.Quit 3
End If

If Not oFSO.FileExists(strScript) Then
    WScript.Echo "[ERRO] monitor_ui.py nao encontrado: " & strScript
    WScript.Quit 4
End If

WScript.Echo "[monitor] Base     : " & strBase
WScript.Echo "[monitor] Python   : " & strPythonW
WScript.Echo "[monitor] Script   : " & strScript
WScript.Echo "[monitor] Lancando UI via Shell.Application (desktop interativo)..."

' ── Lançar pythonw via Shell.Application.ShellExecute ──────────────
' ShellExecute usa o Windows Shell e garante que o processo é lançado
' no DESKTOP INTERATIVO do usuario (WindowStation\Default), independente
' de quem chamou este VBS (Control Panel, Task Scheduler, etc.).
'
' Parametros: file, args, workingDir, verb, showCmd (1 = SW_SHOWNORMAL)
Dim oShellApp
Set oShellApp = CreateObject("Shell.Application")
oShellApp.ShellExecute strPythonW, """" & strScript & """", strBase, "open", 1
Set oShellApp = Nothing

WScript.Echo "[monitor] UI lancada via Shell. VBS encerrando."

Set oShell = Nothing
Set oFSO   = Nothing

WScript.Quit 0
