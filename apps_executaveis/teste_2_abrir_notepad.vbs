' ═══════════════════════════════════════════════════════════════
'  TESTE 2 — Lançar GUI (Notepad) e voltar
'
'  Simula o padrão LAUNCHER (igual ao iniciar_silencioso.vbs do Monitor
'  Contribuintes): lança um processo filho com GUI (notepad.exe) e
'  termina imediatamente via oShell.Run ..., 1, False.
'
'  Se este rodar e o Notepad abrir, o suporte a launchers do Control
'  Panel está funcionando E o Job Object NÃO está matando o filho.
'  Quando fechar o Notepad manualmente, o job do Control Panel deve
'  detectar 'árvore vazia' e marcar como 'done'.
' ═══════════════════════════════════════════════════════════════

Dim oShell
Set oShell = CreateObject("WScript.Shell")

WScript.Echo "[teste_2] Lancando notepad.exe..."

' 1 = janela normal visível, False = não espera terminar
oShell.Run "notepad.exe", 1, False

WScript.Echo "[teste_2] Notepad lancado. Feche a janela manualmente para finalizar este teste."
WScript.Echo "[teste_2] VBS encerrando agora (o notepad continua vivo na arvore)."

Set oShell = Nothing
WScript.Quit 0
