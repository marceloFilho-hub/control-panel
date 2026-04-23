' ═══════════════════════════════════════════════════════════════════
'  Hidra Control Plane — Launcher da Área de Trabalho
'
'  O que faz:
'    1. Detecta a raiz do projeto (pai da pasta scripts/)
'    2. Verifica se o .venv existe — se não, executa install.bat
'    3. Inicia `python -m src.main` sem abrir janela de console
'    4. Aguarda o dashboard responder em http://localhost:9000
'    5. Abre o navegador padrão na URL
'
'  Duplo-clique neste arquivo (ou no atalho do Desktop) → painel sobe.
' ═══════════════════════════════════════════════════════════════════

Option Explicit

Dim oShell, oFSO, oHttp
Dim strRoot, strVenvPy, strInstaller, strCmd, strUrl
Dim i

Set oShell = CreateObject("WScript.Shell")
Set oFSO   = CreateObject("Scripting.FileSystemObject")

' 1. Raiz do projeto = pai de scripts/
strRoot  = oFSO.GetParentFolderName(oFSO.GetParentFolderName(WScript.ScriptFullName))
strVenvPy = strRoot & "\.venv\Scripts\python.exe"
strInstaller = strRoot & "\scripts\install.bat"
strUrl   = "http://localhost:9000"

' 2. Se .venv não existe, chamar instalador primeiro
If Not oFSO.FileExists(strVenvPy) Then
    MsgBox "Ambiente virtual nao encontrado. Executando o instalador primeiro." & vbCrLf & _
           "Isso pode levar alguns minutos na primeira vez.", _
           vbInformation, "Hidra Control Plane"
    oShell.Run """" & strInstaller & """", 1, True  ' 1 = visivel, True = esperar
End If

If Not oFSO.FileExists(strVenvPy) Then
    MsgBox "Falha na instalacao. Verifique se o Python 3.13+ esta instalado.", _
           vbCritical, "Hidra Control Plane"
    WScript.Quit 1
End If

' 3. Verificar se já está rodando (porta 9000 ocupada)
If IsPortOpen(9000) Then
    ' Já rodando — apenas abrir o browser
    oShell.Run """" & strUrl & """", 1, False
    WScript.Quit 0
End If

' 4. Iniciar `python -m src.main` sem console
oShell.CurrentDirectory = strRoot
strCmd = """" & strVenvPy & """ -m src.main"

' 0 = janela oculta (SW_HIDE), False = nao esperar terminar
oShell.Run strCmd, 0, False

' 5. Aguardar o dashboard responder (até 60s)
For i = 1 To 30
    WScript.Sleep 2000
    If IsPortOpen(9000) Then
        Exit For
    End If
Next

If IsPortOpen(9000) Then
    ' Abrir o navegador
    oShell.Run """" & strUrl & """", 1, False
Else
    MsgBox "O painel demorou mais que o esperado para iniciar." & vbCrLf & _
           "Verifique os logs em: " & strRoot & "\logs\hidra_control.log", _
           vbExclamation, "Hidra Control Plane"
End If

Set oShell = Nothing
Set oFSO   = Nothing
WScript.Quit 0


' ═══════════════════════════════════════════════════════════════════
'  Helper: testa se uma porta TCP local responde (via Msxml2.ServerXMLHTTP)
' ═══════════════════════════════════════════════════════════════════
Function IsPortOpen(port)
    Dim oReq
    On Error Resume Next
    Set oReq = CreateObject("Msxml2.ServerXMLHTTP.6.0")
    oReq.setTimeouts 1000, 1000, 1000, 1000
    oReq.open "GET", "http://localhost:" & port & "/", False
    oReq.send
    If Err.Number = 0 And oReq.status > 0 Then
        IsPortOpen = True
    Else
        IsPortOpen = False
    End If
    Err.Clear
    On Error GoTo 0
    Set oReq = Nothing
End Function
