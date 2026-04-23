' ═══════════════════════════════════════════════════════════════
'  TESTE 1 — Hello silencioso
'
'  O mais básico possível: escreve no stdout, termina com exit 0.
'  Se este NÃO rodar, o problema é no Control Panel (subprocess/shell).
'  Se rodar (aparece no log "Ao vivo" com a mensagem), a camada de
'  execução de VBS do painel está OK.
' ═══════════════════════════════════════════════════════════════

WScript.Echo "[teste_1] Ola do VBS! Timestamp: " & Now()
WScript.Echo "[teste_1] Hostname: " & CreateObject("WScript.Network").ComputerName
WScript.Echo "[teste_1] Diretorio: " & CreateObject("Scripting.FileSystemObject").GetAbsolutePathName(".")
WScript.Quit 0
