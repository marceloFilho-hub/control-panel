' ═══════════════════════════════════════════════════════════════════
'  Hidra Control Plane — Launcher da Área de Trabalho
'
'  O que faz:
'    1. Detecta a raiz do projeto (pai da pasta scripts/)
'    2. Verifica se o .venv existe — se não, executa install.bat
'    3. Detecta se o orchestrator JÁ está vivo (via state.json mtime
'       recente — confirma que não é só um Streamlit zumbi)
'    4. Se não está: mata qualquer resíduo na porta 9000 + Streamlit
'       órfãos + python rodando src.main
'    5. Inicia `python -m src.main` sem abrir janela de console
'    6. Aguarda o dashboard responder em http://localhost:9000
'    7. Abre o navegador padrão na URL
'
'  Duplo-clique neste arquivo (ou no atalho do Desktop) → painel sobe.
' ═══════════════════════════════════════════════════════════════════

Option Explicit

Dim oShell, oFSO
Dim strRoot, strPy, strInstaller, strCmd, strUrl
Dim i

Set oShell = CreateObject("WScript.Shell")
Set oFSO   = CreateObject("Scripting.FileSystemObject")

' 1. Raiz do projeto = pai de scripts/
strRoot  = oFSO.GetParentFolderName(oFSO.GetParentFolderName(WScript.ScriptFullName))
strInstaller = strRoot & "\scripts\install.bat"
strUrl   = "http://localhost:9000"

' 2. Detectar Python (prioridade: .venv do projeto → python global)
strPy = DetectPython(strRoot)

If strPy = "" Then
    ' Ultima tentativa: rodar o instalador
    Dim resp
    resp = MsgBox("Python nao encontrado no sistema nem em .venv." & vbCrLf & vbCrLf & _
                  "Deseja executar o instalador agora?" & vbCrLf & _
                  "(Isso vai criar um .venv e instalar as dependencias.)", _
                  vbYesNo + vbQuestion, "Hidra Control Plane")
    If resp = vbYes Then
        oShell.Run """" & strInstaller & """", 1, True
        strPy = DetectPython(strRoot)
    End If
End If

If strPy = "" Then
    MsgBox "Nao foi possivel iniciar o painel: Python 3.13+ nao encontrado.", _
           vbCritical, "Hidra Control Plane"
    WScript.Quit 1
End If

' 3. Detectar se o orchestrator está REALMENTE vivo.
'    Critério: state.json foi atualizado nos últimos 30s.
'    (O orchestrator salva a cada 5s; se está estagnado > 30s, morreu.)
If IsOrchestratorAlive(strRoot) Then
    ' Já rodando — só abrir o browser
    oShell.Run """" & strUrl & """", 1, False
    WScript.Quit 0
End If

' 4. Cleanup de resíduos (Streamlit/python órfãos na porta 9000 e que
'    estavam rodando src.main mas cujo orquestrador morreu).
CleanupZombies strRoot

' 5. Iniciar `python -m src.main` sem console
oShell.CurrentDirectory = strRoot
strCmd = """" & strPy & """ -m src.main"
oShell.Run strCmd, 0, False

' 6. Aguardar o dashboard responder (até 60s)
For i = 1 To 30
    WScript.Sleep 2000
    If IsPortOpen(9000) Then
        Exit For
    End If
Next

If IsPortOpen(9000) Then
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
'  Helper: detecta o Python a usar.
'  Prioridade:
'    1. .venv do projeto
'    2. python3.13.exe no PATH
'    3. python.exe no PATH (se versao >= 3.13)
'  Retorna o caminho absoluto ou "" se não achar.
' ═══════════════════════════════════════════════════════════════════
Function DetectPython(root)
    Dim venvPy
    DetectPython = ""

    ' 1. venv do projeto
    venvPy = root & "\.venv\Scripts\python.exe"
    If oFSO.FileExists(venvPy) Then
        DetectPython = venvPy
        Exit Function
    End If

    ' 2. python3.13 no PATH
    Dim path313
    path313 = ResolveInPath("python3.13.exe")
    If path313 <> "" Then
        DetectPython = path313
        Exit Function
    End If

    ' 3. python no PATH (valida versao >= 3.13)
    Dim pathPy
    pathPy = ResolveInPath("python.exe")
    If pathPy <> "" Then
        If PythonVersionOk(pathPy) Then
            DetectPython = pathPy
            Exit Function
        End If
    End If
End Function

' Helper: resolve caminho absoluto de um exe no PATH (equivalente ao `where`)
Function ResolveInPath(exeName)
    Dim oExec, result
    ResolveInPath = ""
    On Error Resume Next
    Set oExec = oShell.Exec("cmd /c where " & exeName)
    If Err.Number <> 0 Then
        On Error GoTo 0
        Exit Function
    End If
    Do While oExec.Status = 0
        WScript.Sleep 50
    Loop
    result = oExec.StdOut.ReadAll
    On Error GoTo 0
    If Len(result) > 0 Then
        ' Pegar a primeira linha (primeiro hit)
        Dim firstLine
        firstLine = Split(result, vbCrLf)(0)
        firstLine = Trim(firstLine)
        ' Ignorar o stub do MS Store WindowsApps que só redireciona pra loja
        If InStr(LCase(firstLine), "microsoft\windowsapps") = 0 And oFSO.FileExists(firstLine) Then
            ResolveInPath = firstLine
        End If
    End If
End Function

' Helper: verifica se o python informado é >= 3.13
Function PythonVersionOk(pyPath)
    Dim oExec
    PythonVersionOk = False
    On Error Resume Next
    Set oExec = oShell.Exec("""" & pyPath & """ -c ""import sys; sys.exit(0 if sys.version_info >= (3, 13) else 1)""")
    Do While oExec.Status = 0
        WScript.Sleep 50
    Loop
    PythonVersionOk = (oExec.ExitCode = 0)
    On Error GoTo 0
End Function

' ═══════════════════════════════════════════════════════════════════
'  Helper: testa se uma porta TCP local responde
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

' ═══════════════════════════════════════════════════════════════════
'  Helper: orchestrator vivo = state.json foi escrito nos últimos 30s
' ═══════════════════════════════════════════════════════════════════
Function IsOrchestratorAlive(root)
    Dim stateFile, f, ageSec
    stateFile = root & "\state.json"
    If Not oFSO.FileExists(stateFile) Then
        IsOrchestratorAlive = False
        Exit Function
    End If
    Set f = oFSO.GetFile(stateFile)
    ageSec = DateDiff("s", f.DateLastModified, Now)
    IsOrchestratorAlive = (ageSec >= 0 And ageSec <= 30)
End Function

' ═══════════════════════════════════════════════════════════════════
'  Helper: mata Streamlit/python órfãos que possam ocupar a porta 9000
'  ou que estavam rodando src.main mas cujo orquestrador morreu.
' ═══════════════════════════════════════════════════════════════════
Sub CleanupZombies(root)
    Dim oWMI, oProcs, p, cmdLine
    On Error Resume Next
    Set oWMI = GetObject("winmgmts:\\.\root\cimv2")
    If oWMI Is Nothing Then Exit Sub

    Set oProcs = oWMI.ExecQuery( _
        "SELECT ProcessId, Name, CommandLine FROM Win32_Process " & _
        "WHERE Name='python.exe' OR Name='python3.13.exe' OR Name='pythonw.exe'")

    For Each p In oProcs
        cmdLine = LCase(p.CommandLine & "")
        ' Mata apenas processos relacionados ao Control Panel deste projeto
        If InStr(cmdLine, "streamlit") > 0 And InStr(cmdLine, LCase(root)) > 0 Then
            p.Terminate()
        ElseIf InStr(cmdLine, "src.main") > 0 And InStr(cmdLine, LCase(root)) > 0 Then
            p.Terminate()
        ElseIf InStr(cmdLine, LCase(root & "\src\ui\dashboard.py")) > 0 Then
            p.Terminate()
        End If
    Next

    ' Pequena pausa pra SO soltar a porta
    WScript.Sleep 1500

    Set oWMI = Nothing
    On Error GoTo 0
End Sub


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
