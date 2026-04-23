' ═══════════════════════════════════════════════════════════════
'  TESTE 3 — Processamento demorado com output periódico
'
'  Roda por ~15 segundos, imprimindo uma linha por segundo.
'  Útil para verificar:
'    - O stream de stdout em tempo real na aba "Ao vivo"
'    - O monitoramento de CPU/Memória durante execução
'    - A barra de progresso de status running
'    - O cleanup ao final (exit 0 limpo)
' ═══════════════════════════════════════════════════════════════

Dim i, iterations
iterations = 15

WScript.Echo "[teste_3] Iniciando processamento de " & iterations & " ciclos..."

For i = 1 To iterations
    WScript.Echo "[teste_3] Ciclo " & i & "/" & iterations & " em " & Time()
    ' Sleep 1 segundo
    WScript.Sleep 1000
Next

WScript.Echo "[teste_3] Concluido! Todos os " & iterations & " ciclos OK."
WScript.Quit 0
