# Pasta de executáveis do Control Panel

Esta é a pasta **monitorada automaticamente** pelo Control Panel. Tudo que você
soltar aqui aparece na aba **⚙️ Configurar** do dashboard, pronto para ser
ativado com um clique.

## Tipos de arquivo suportados

| Extensão | Exemplo | Como é executado |
|----------|---------|------------------|
| `.vbs` | `meu_robo.vbs` | `cscript //nologo //B "meu_robo.vbs"` (silencioso, sem janela) |
| `.exe` | `app.exe` | invocação direta |
| `.bat` / `.cmd` | `tarefa.bat` | `cmd /c "tarefa.bat"` |
| `.ps1` | `script.ps1` | `powershell -ExecutionPolicy Bypass -File "script.ps1"` |
| `.py` | `automacao.py` | `.venv/Scripts/python` (se existir na pasta pai) ou `python` global |
| `.lnk` | `atalho.lnk` | resolve o target real |

## Como usar

1. **Crie um wrapper** `.vbs` (recomendado) ou copie/lincie o executável aqui.
2. **Abra o dashboard** em http://localhost:9000 → aba ⚙️ Configurar.
3. **Marque a checkbox** "Executar este app".
4. **Defina o tempo** entre rodagens e o slot (heavy/light/always).
5. **Pronto.** O Control Panel cuida do resto: tempo, fila, memória, logs.

## Por que `.vbs`?

Wrappers `.vbs` são a forma mais limpa de invocar processos no Windows
**sem abrir janela de console**. Exemplo de wrapper para um app Python:

```vbs
' meu_robo.vbs — invoca o app Python sem janela
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\Rotinas\Documents\Projetos\meu_projeto"
WshShell.Run ".venv\Scripts\python.exe src\main.py", 0, True
```

Cole isso num arquivo `meu_robo.vbs` aqui na pasta e ele aparece no dashboard.

## Importante

- **Não commite arquivos sensíveis** aqui (use `.gitignore` se necessário).
- Os settings de cada app (tempo entre rodagens, slot, RAM, etc.) são salvos
  em `config.yaml` — esse arquivo é a memória persistente do dashboard.
- Apagar um arquivo daqui o **remove** automaticamente do dashboard no
  próximo scan (em até 5 segundos).
