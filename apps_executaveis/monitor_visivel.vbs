' ═══════════════════════════════════════════════════════════════
'  Monitor de Contribuintes — Launcher VISÍVEL (alternativa)
'
'  Mesmo fluxo do iniciar_silencioso.vbs, mas lança pythonw.exe com
'  janela VISÍVEL (Run ..., 1, False em vez de ..., 0, False).
'
'  Use este se quiser ver a UI do Monitor abrindo via Control Panel.
' ═══════════════════════════════════════════════════════════════

Option Explicit

Dim oShell, oFSO
Dim strBase, strPythonW, strScript, strCmd

Set oShell = CreateObject("WScript.Shell")
Set oFSO   = CreateObject("Scripting.FileSystemObject")

' Caminho fixo do projeto (já validado que existe)
strBase    = "C:\Users\marcelo.lsantos_bhub\Documents\automacoes\cadastro_contribuinte"
strPythonW = strBase & "\.venv\Scripts\pythonw.exe"
strScript  = strBase & "\src\front\monitor_ui.py"

If Not oFSO.FileExists(strPythonW) Then
    WScript.Echo "[ERRO] pythonw.exe nao encontrado em: " & strPythonW
    WScript.Quit 1
End If

If Not oFSO.FileExists(strScript) Then
    WScript.Echo "[ERRO] monitor_ui.py nao encontrado em: " & strScript
    WScript.Quit 1
End If

WScript.Echo "[monitor_visivel] Lancando UI do Monitor de Contribuintes..."
WScript.Echo "[monitor_visivel] Base:   " & strBase
WScript.Echo "[monitor_visivel] Python: " & strPythonW
WScript.Echo "[monitor_visivel] Script: " & strScript

' CurrentDirectory importante pra imports relativos do Python
oShell.CurrentDirectory = strBase

' Comando com aspas preservadas
strCmd = """" & strPythonW & """ """ & strScript & """"

' 1 = SW_SHOWNORMAL (janela visível); False = não esperar terminar
oShell.Run strCmd, 1, False

WScript.Echo "[monitor_visivel] UI lancada. O VBS vai encerrar, mas o pythonw.exe segue rodando."
WScript.Echo "[monitor_visivel] Feche a janela do Monitor para finalizar esta execucao."

Set oShell = Nothing
Set oFSO   = Nothing

WScript.Quit 0
