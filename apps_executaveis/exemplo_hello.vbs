' exemplo_hello.vbs — wrapper VBScript de teste
'
' Imprime uma linha no stdout e termina com exit code 0.
' Execução silenciosa (sem janela) via: cscript //nologo //B "exemplo_hello.vbs"
'
' Use este arquivo como referência para criar seus próprios wrappers
' que invocam seus apps reais. Exemplo para chamar um projeto Python:
'
'   Set WshShell = CreateObject("WScript.Shell")
'   WshShell.CurrentDirectory = "C:\caminho\do\seu\projeto"
'   WshShell.Run ".venv\Scripts\python.exe src\main.py", 0, True
'
' (o terceiro argumento True = aguardar término antes de retornar)

WScript.Echo "Olá do exemplo_hello.vbs — " & Now()
WScript.Echo "Este e um wrapper VBScript de teste para o Control Panel."
WScript.Quit 0
