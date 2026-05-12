# Hidra Control Plane

> Orquestrador central de automações Python para VMs Windows — sem Docker, sem cron, com memória consciente.
>
> **Desenvolvido por Marcelo Leandro dos Santos Filho**

[![License](https://img.shields.io/badge/license-proprietary-blue.svg)]()
[![Python](https://img.shields.io/badge/python-3.13+-green.svg)]()
[![Platform](https://img.shields.io/badge/platform-Windows-lightgrey.svg)]()
[![Status](https://img.shields.io/badge/status-production-success.svg)]()

---

## Sumário

- [O problema](#o-problema)
- [Decisões arquiteturais](#decisões-arquiteturais)
- [Stack](#stack)
- [Arquitetura](#arquitetura)
- [Como rodar](#como-rodar)
- [Cadastro de apps](#cadastro-de-apps)
- [Auto-update via git](#auto-update-via-git)
- [Orquestração e filas](#orquestração-e-filas)
- [Cleanup e Job Objects](#cleanup-e-job-objects)
- [Cleanup pós-execução e da VM](#cleanup-pós-execução-e-da-vm)
- [Persistência](#persistência)
- [Hot reload](#hot-reload)
- [Logs e observabilidade](#logs-e-observabilidade)
- [API interna (comandos)](#api-interna-comandos)
- [Estrutura do projeto](#estrutura-do-projeto)
- [Configurações avançadas](#configurações-avançadas)
- [Deploy em VM](#deploy-em-vm)
- [Troubleshooting](#troubleshooting)
- [Limitações conhecidas](#limitações-conhecidas)

---

## O problema

Numa VM Windows com 16 GB de RAM e 4 vCPUs rodando ~16 automações Python
(RPA, ETL, monitores de email, GUIs Tkinter), três abordagens comuns falham:

| Abordagem | Problema |
|---|---|
| **Docker Desktop + WSL2** | 1.5–2.5 GB só de overhead, COM/pywinauto incompatíveis com Linux |
| **Windows Task Scheduler** | Sem controle de concorrência, sem visibilidade, sem limite de RAM |
| **Scripts `.bat` soltos** | Zero observabilidade, processos zumbi, congestionamento silencioso |

O Hidra Control Plane resolve com **~80 MB de overhead**, cadastro visual,
cleanup atômico via Windows Job Objects e orquestração **memory-aware**
que impede o sistema de saturar.

---

## Decisões arquiteturais

### 1. Sem cron / sem horário fixo

O orquestrador **não agenda por hora** (07:00, 15:30...). Agenda por
**tempo entre rodagens** (`pause_between`). A diferença é crítica:

- **Cron** assume execução pontual. Se um job demora mais que o intervalo,
  você tem overlap ou misfires.
- **pause_between** é `fim_rodada_N + pause → início_rodada_N+1`.
  Impossível overlap do mesmo app. Ciclos naturalmente espaçados.

### 2. Apps efêmeros por padrão

Apps `loop` rodam → terminam → dormem → voltam. Entre execuções,
**consumo de RAM é zero**. O orquestrador é quem segura o relógio,
não cada app.

### 3. Memory-aware scheduling

Antes de iniciar qualquer app, o orquestrador calcula:

```
disponível = psutil.virtual_memory().available
utilizável = disponível − ram_safety_margin_mb (default 512 MB)
pode_rodar = utilizável >= app.max_ram_mb
```

Se `pode_rodar == False`, o app entra na `memory_queue` e aguarda via
`asyncio.Event` — acorda assim que outro app libera RAM. Evita OOM,
swap thrashing e crashes em cascata.

### 4. Windows Job Objects para cleanup

Cada app é associado a um Job Object com `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.
Ao final (normal, timeout, kill manual), `TerminateJobObject` mata **toda
a árvore atomicamente** — inclusive netos órfãos, browsers Selenium
desanexados e subshells. É a mesma técnica usada pelo Docker/VS Code.

### 5. Estado em YAML + JSON

- `config.yaml` → cadastros permanentes (editável, versionável, human-readable)
- `state.json` → runtime (recriado do zero se perder; não é source of truth)
- `logs/{app}/history.jsonl` → histórico persistente (JSONL = append-only)

Nada de SQLite/Parquet — escala suficiente pra 100s de apps e elimina
complexidade de migração.

---

## Stack

| Camada | Tecnologia | Motivo |
|---|---|---|
| Runtime | Python 3.13 | asyncio, type hints modernos, pattern matching |
| Async | `asyncio` | Single-threaded event loop, sem GIL contention |
| UI | Streamlit 1.54 | Dashboard web em Python, sem frontend JS |
| Refresh UI | `st.fragment(run_every=N)` | Isolado por aba, sem re-render global |
| Processos | `subprocess_shell` + Windows Job Objects (pywin32) | Desktop interativo + cleanup atômico |
| Monitoramento | `psutil` | RAM/CPU por PID + children recursivo |
| Config | `pyyaml` | Human-readable, versionável |
| Logs | `loguru` | Structured logging + rotation |
| Alertas | Telegram Bot API (`httpx`) | Notificação assíncrona de falhas |
| Env vars | `python-dotenv` | `.env` por projeto no subprocess |

---

## Arquitetura

### Visão geral

```
┌─────────────────────────────────────────────────────────────────┐
│                       Hidra Control Plane                         │
│                                                                   │
│  ┌──────────────┐        ┌────────────────────────────────────┐ │
│  │  main.py     │───────▶│       Orchestrator                   │ │
│  │  entry       │        │  • Semaphores (heavy=1, light=3)    │ │
│  └──────┬───────┘        │  • Memory gate (asyncio.Event)       │ │
│         │                 │  • Hot reload (config.yaml mtime)    │ │
│         │                 │  • Comandos via .trigger files       │ │
│         ▼                 └──────────────┬───────────────────────┘ │
│  ┌──────────────┐                        │                         │
│  │ Streamlit    │                        ▼                         │
│  │ dashboard    │              ┌──────────────────┐                │
│  │ :9000        │              │  ProcessManager  │                │
│  │              │              │  • Job Object    │                │
│  │ Abas:        │              │  • subprocess    │                │
│  │  • Status    │              │  • psutil watch  │                │
│  │  • Ao vivo   │              │  • .env loader   │                │
│  │  • Fila      │              └────────┬─────────┘                │
│  │  • Configurar│                       │                         │
│  │  • Histórico │                       ▼                         │
│  └──────┬───────┘              ┌──────────────────┐                │
│         │                       │  App (Python)    │                │
│         │                       │  + filhos        │                │
│         │                       │  (dentro do Job) │                │
│         │                       └──────────────────┘                │
│         │                                                           │
│  ┌──────┴─────────────────────────────────────────────────────┐   │
│  │         Storage (per-machine, local)                        │   │
│  │   config.yaml    state.json    logs/{app}/history.jsonl     │   │
│  └───────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### Fluxo de execução de um app

```
 1. Usuário clica Start no dashboard
         │
         ▼
 2. send_command("start", app_name) → commands/start_{app}.trigger
         │
         ▼
 3. Orchestrator._monitor_loop (5s tick) lê triggers
         │
         ▼
 4. _enable_app(app) cria task asyncio:
         • _run_loop_job (se schedule=loop)
         • _run_always_service (se slot=always)
         • _run_job (se manual)
         │
         ▼
 5. _run_job:
    5a. slot semaphore.acquire()    ← fila por slot
    5b. _wait_for_memory()           ← fila por RAM
    5c. _auto_update_for_app()       ← GLOBAL (settings.auto_update)
        • discover_repo(cwd) — sobe pais até .git
        • lock por repo_root resolvido (serializa pulls do mesmo repo)
        • skip se outro app vivo no mesmo repo
        • git fetch + git reset --hard <remote>/<branch>
        • falha → on_failure=abort_cycle libera slot, próximo ciclo tenta
    5d. ProcessManager.start()
        • abre ExecutionLogger + gera run_id
        • _run_pre_start_hooks(cwd, env)   ← pre_start[] por app
            – pre_start[] sequencial (prefixo [pre])
            – {python}/{pip} resolvidos do .venv
            – timeout cumulativo (pre_start_timeout)
            – aborta se pre_start_required e algum cmd falha
        • cria Job Object
        • subprocess_shell com creationflags (se gui=true)
        • assign PID ao Job
        • stream stdout/stderr → execution_logger
         │
         ▼
 6. wait_with_monitoring:
    • monitora RAM/CPU/timeout
    • se excede max_ram → kill
    • se excede timeout → kill
    • aguarda processo principal + descendentes
         │
         ▼
 7. _finalize:
    • TerminateJobObject (mata árvore)
    • kill_process_tree (failsafe psutil)
    • gc.collect()
    • log RAM liberada
         │
         ▼
 8. Se loop: sleep(pause_between), volta pro passo 5
```

---

## Como rodar

### Requisitos

- Windows 10/11 ou Server 2019+
- Python 3.13 (recomendo o oficial — não use o MS Store sem testar)
- 4+ GB RAM livre (16 GB é o sweet spot)

### Instalação assistida (recomendado)

Depois de clonar o repositório, rode o instalador:

```bash
git clone https://github.com/marceloFilho-hub/control-panel.git
cd control-panel
scripts\install.bat
```

O script faz **tudo em 4 passos automatizados**:

1. Valida que o Python 3.13+ está instalado
2. Cria `.venv` na raiz do projeto
3. Instala dependências via `pip install -e .`
4. Cria um atalho **"Hidra Control Plane"** na Área de Trabalho

Ao final, oferece iniciar o painel imediatamente. A partir daí basta
**duplo-clique no atalho do desktop** — o `iniciar_painel.vbs`:

- Sobe o `python -m src.main` sem janela de console (SW_HIDE)
- Faz polling de `http://localhost:9000` até o dashboard responder
- Abre o navegador padrão direto na URL
- Se o painel já estiver rodando, apenas abre o browser (idempotente)

### Instalação manual (alternativa)

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e .
python -m src.main
```

### Desinstalação

```bash
scripts\desinstalar.bat
```

Remove o atalho da Área de Trabalho e mata o processo na porta 9000.
**Não apaga** código, config ou logs.

### Primeira execução

O `_ensure_config_exists` copia `config.example.yaml` → `config.yaml` se
não existir. Acesse o dashboard em **http://localhost:9000**.

### .env opcional (para alertas Telegram)

```bash
# .env na raiz do projeto
TELEGRAM_BOT_TOKEN=123456:ABC-def
TELEGRAM_CHAT_ID=-100123456789
```

---

## Cadastro de apps

O único modo de cadastro é via dashboard → aba **⚙️ Configurar** →
seção **🐍 Cadastrar app Python**:

1. Cole o caminho do script principal (ex: `C:/proj/meu_robo/src/main.py`)
2. O Control Panel detecta automaticamente em até 5 pastas ancestrais:
   - `.venv/Scripts/python.exe` e `pythonw.exe`
   - `.env`
3. Marque **📺 GUI** se o app tem janela (Tkinter/PyQt/wxPython)
4. Defina `slot`, `max_ram_mb`, argumentos, tempo entre rodagens

O `config.yaml` resultante:

```yaml
apps:
  meu_robo:
    slot: heavy
    cwd: C:/proj/meu_robo
    cmd: '"C:/proj/meu_robo/.venv/Scripts/python.exe" "C:/proj/meu_robo/src/main.py"'
    schedule: loop
    pause_between: 600          # 10 min entre rodagens
    max_ram_mb: 1200
    timeout: 3600
    env_file: C:/proj/meu_robo/.env
    gui: false
    _source: python
```

### Campos do config

| Campo | Tipo | Default | Descrição |
|---|---|---|---|
| `slot` | `heavy`/`light`/`always` | `light` | Fila de concorrência |
| `cmd` | string | — | Linha de comando completa (com quoting) |
| `cwd` | string | — | Working directory do subprocess |
| `schedule` | `manual`/`loop` | `manual` | Modo de execução |
| `pause_between` | int (segundos) | 0 | Tempo após fim para próxima rodada |
| `max_ram_mb` | int | 1024 | Limite de RAM (processo + descendentes) |
| `timeout` | int (segundos) | 600 | Tempo máx de uma execução |
| `env_file` | string | — | Path do `.env` a carregar no subprocess |
| `auto_start` | bool | false | Inicia junto com o orquestrador |
| `restart_on_crash` | bool | false | Apenas para `slot: always` |
| `gui` | bool | false | `CREATE_BREAKAWAY_FROM_JOB` para janelas |
| `pre_start` | list[str] | `[]` | Comandos shell sequenciais executados no `cwd` antes do app — suporta `{python}`/`{pip}` |
| `pre_start_timeout` | int (segundos) | 300 | Timeout cumulativo para os `pre_start` |
| `pre_start_required` | bool | true | Se true, falha em qualquer `pre_start` aborta a rodada; se false, apenas loga |
| `kill_orphans` | list[str] | `[]` | Nomes de processos extras (case-insensitive) a matar após cada rodada deste app, além dos drivers default — ver [Cleanup pós-execução](#cleanup-pós-execução-e-da-vm) |

> O campo `git_pull: true` por app é **deprecated** e ignorado em runtime.
> Auto-update via git agora é controlado em `settings.auto_update` (ver
> seção [Auto-update via git](#auto-update-via-git) abaixo). Configs
> antigas continuam carregando sem erro.

### Auto-update via git

Antes de cada rodada, com slot e gate de RAM já adquiridos e antes de
iniciar o subprocess, o orquestrador descobre o repo a partir do `cwd`
do app e roda `git fetch + git reset --hard <remote>/<branch>`.
**Não há configuração por app** — a feature é responsabilidade do
orquestrador.

```yaml
settings:
  auto_update:
    enabled: true             # liga/desliga global
    on_failure: abort_cycle   # ou skip_update
    timeout_seconds: 60       # total para fetch + reset somados
    skip_paths: []            # lista de repo_root absolutos a ignorar
```

Como funciona:

- **Detecção do repo**: sobe os pais do `cwd` até achar `.git`.
  Funciona com `cwd` apontando para subpasta (ex: `repo/src/entrypoints`).
- **Branch**: `git rev-parse --abbrev-ref HEAD` no repo root —
  usa o que está checked out, não hardcoded `main`.
- **Remote**: `branch.<branch>.remote` (com fallback `origin`).
  Sem remote configurado → no-op silencioso.
- **Lock por repo_root resolvido**: se dois apps compartilham o
  mesmo repo (ex: futuro `monitor_*` + `executor_*` no mesmo projeto),
  só um faz pull por vez.
- **Skip se outro app do mesmo repo está vivo**: pull não pode
  rodar enquanto arquivos do repo estão em uso. Loga warning,
  segue com a versão em disco. Próximo ciclo tenta de novo.
- **Falha com `on_failure: abort_cycle`** (default): libera slot,
  alerta no Telegram, próximo ciclo tenta de novo. **Nunca roda
  app silenciosamente com versão antiga após erro.**
- **`skip_paths`**: lista de paths absolutos de repo_root a ignorar
  (comparação por `Path.resolve()`). Útil para repos em desenvolvimento
  ativo na VM que não devem ser sobrescritos.

### Hooks pré-execução (`pre_start`)

Cada app pode declarar comandos shell executados **antes** do subprocess
principal, dentro do mesmo `run_id` e com a saída unificada no `.log` da
execução (prefixo `[pre]`):

```yaml
apps:
  meu_robo:
    cwd: C:/proj/meu_robo
    cmd: '"C:/proj/meu_robo/.venv/Scripts/python.exe" "src/main.py"'
    pre_start:
      - "{python} -m pip install -r requirements.txt"
      - "{python} scripts/migrate.py"
    pre_start_timeout: 300          # total dos pre_start somados
    pre_start_required: true        # falha aborta a rodada
```

- **`pre_start`** roda na ordem declarada via `subprocess_shell` no `cwd`,
  herdando o `env` do app (incluindo `.env` carregado).
- **`{python}`** e **`{pip}`** são substituídos por
  `cwd/.venv/Scripts/python.exe` e `pip.exe` quando existem; caso
  contrário, recaem em `python` / `python -m pip` do PATH.
- O `pre_start_timeout` é **cumulativo** entre os comandos.
- O auto-update via git roda **antes** do `pre_start` (na fase de
  orquestração), então comandos de `pre_start` já trabalham com o
  código atualizado.
- Configurável também pela UI: aba **⚙️ Configurar** → expander
  **🪝 Hooks pré-execução** em cada card de app.

---

## Orquestração e filas

### 8 camadas de proteção

1. **Semáforos por slot**: `heavy=1`, `light=3`, `always=∞`
2. **Fila FIFO rastreável**: ordem de entrada respeitada, visível no dashboard
3. **Memory gate**: bloqueia início se `available − safety < max_ram`
4. **Sem overlap**: `status=running` → próxima chamada é pulada
5. **Timeout**: mata árvore se ultrapassa limite
6. **RAM cap**: mata árvore se processo infla
7. **pause_between**: espera APÓS o fim (sem conflito de horário)
8. **Cleanup atômico**: Job Object + kill_tree + gc.collect

### Filas

```python
# Em orchestrator.py
_heavy_queue: list[str]   # FIFO por slot heavy
_light_queue: list[str]   # FIFO por slot light
_memory_queue: list[str]  # Apps com slot mas sem RAM
```

### Memory gate (asyncio.Event)

```python
async def _wait_for_memory(self, app_name, required_mb):
    while enabled:
        available = psutil.virtual_memory().available_mb
        usable = available - safety_margin
        if usable >= required_mb:
            return
        self._memory_queue.append(app_name)
        await asyncio.wait_for(self._memory_event.wait(), timeout=5.0)
        self._memory_event.clear()
```

Quando um app termina, `_finalize` dispara `_release_memory_event()`
que desperta todos os aguardadores — re-avaliam em microssegundos.

---

## Cleanup e Job Objects

### Por que Job Objects

Sem Job Object, apps que spawnam processos desanexados (Selenium/Chrome,
Celery workers, subshells) deixam **órfãos** — o parent morre mas os
filhos continuam rodando, consumindo RAM indefinidamente.

### Implementação

```python
# src/windows_job.py
from win32job import CreateJobObject, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

job = CreateJobObject(None, f"ControlPanel_{app_name}_{ts}")
info = QueryInformationJobObject(job, JobObjectExtendedLimitInformation)
info["BasicLimitInformation"]["LimitFlags"] |= JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
SetInformationJobObject(job, JobObjectExtendedLimitInformation, info)
AssignProcessToJobObject(job, proc_handle)

# No final:
TerminateJobObject(job, 1)  # mata toda a árvore atomicamente
```

### Exceção: apps GUI

Apps com `gui: true` **não** entram em Job Object. Motivo: processos
em Job Objects podem ficar presos num WindowStation inválido e janelas
não aparecem no desktop interativo. Solução:

```python
creationflags = (
    0x01000000   # CREATE_BREAKAWAY_FROM_JOB
    | 0x00000200 # CREATE_NEW_PROCESS_GROUP
    | 0x00000008 # DETACHED_PROCESS
)
```

Para GUIs, o cleanup é via `psutil.kill_process_tree()` ao fechar a janela.

---

## Cleanup pós-execução e da VM

O Job Object cobre 99% dos casos, mas após dias de uso a VM ainda
acumulava lixo: drivers Selenium/Playwright que escapavam do Job,
pastas `__pycache__` espalhadas pelos repos atualizados via auto-update,
e o próprio `%TEMP%` crescendo sem limite. Em produção, uma VM com 10 GB
livres chegava a ~1 GB em 24h.

O módulo `src/process/cleanup.py` resolve isso em duas frentes.

### Cleanup automático por rodada (per-app)

Após o `_finalize()` matar a árvore do app, o orquestrador chama
`per_app_cleanup(app)`, que faz:

1. **Kill por nome** dos drivers default (`DEFAULT_ORPHAN_NAMES`):
   - `chromedriver.exe`, `geckodriver.exe`, `msedgedriver.exe`,
     `iedriver.exe`, `operadriver.exe`, `playwright-headless-shell.exe`
   - Somados aos nomes extras declarados em `kill_orphans` do app.
2. **Purga `__pycache__`** recursiva no `cwd` do app, pulando pastas
   pesadas/protegidas: `.venv`, `.git`, `node_modules`, `site-packages`.

**Não toca em `%TEMP%` nem na Lixeira** — o custo por rodada precisa ser
baixo. Essa limpeza profunda fica para o cleanup completo (abaixo).

As métricas da última execução ficam no `AppState`:

- `last_cleanup_mb` — MB liberados pelo `__pycache__` purgado
- `last_cleanup_orphans` — quantos drivers órfãos foram mortos

#### Configurando `kill_orphans` por app

```yaml
apps:
  meu_robo_selenium:
    cmd: '"...\python.exe" "src/main.py"'
    cwd: C:/proj/meu_robo
    kill_orphans:
      - "minha_extensao.exe"   # case-insensitive
      - "headless_helper.exe"
```

Também editável pela UI: aba **⚙️ Configurar** → expander
**🧹 Cleanup pós-execução** em cada card de app.

### Cleanup completo sob demanda (botão "🧹 Limpar VM")

A aba **Status** do dashboard tem um botão **🧹 Limpar VM** que dispara
`full_cleanup()`:

1. Mata órfãos do `DEFAULT_ORPHAN_NAMES` **+ união** dos `kill_orphans`
   de todos os apps configurados.
2. Apaga `__pycache__` de **todos** os `cwd` registrados no `config.yaml`.
3. Limpa `%TEMP%`, `%LOCALAPPDATA%\Temp` e `C:\Windows\Temp` removendo
   apenas arquivos com `mtime > 5 min` — janela de segurança para não
   pisar em apps rodando que estão escrevendo arquivos temporários.
4. Esvazia a Lixeira do Windows via PowerShell `Clear-RecycleBin -Force`.

**Não mexe com standby memory.** Limpar standby exige privilégio de
admin (`SeProfileSingleProcessPrivilege` via `EmptyStandbyList.exe` ou
similar) — decisão arquitetural foi evitar elevação para manter o painel
rodável como usuário comum.

O resultado aparece no rodapé da aba Status como:

```
🧹 Último cleanup completo às 14:32:07: 23 órfãos mortos · 412 MB libertos
```

### API do módulo

```python
from src.process.cleanup import (
    DEFAULT_ORPHAN_NAMES,    # tupla com os 6 drivers default
    per_app_cleanup,         # chamado em _finalize por app
    full_cleanup,            # acionado pelo botão "Limpar VM"
    kill_orphans_by_name,    # mata processos por nome (case-insensitive)
    purge_pycache,           # remove __pycache__ recursivo
    purge_temp_dirs,         # %TEMP%, %LOCALAPPDATA%\Temp, C:\Windows\Temp
    clear_recycle_bin,       # PowerShell Clear-RecycleBin -Force
)
```

---

## Persistência

```
control_panel/
├── config.yaml              # cadastros (por máquina, NO gitignore)
├── config.example.yaml      # template (versionado)
├── state.json               # runtime (recriado automaticamente)
├── state.tmp.<pid>.<tid>    # tmp por thread (evita race)
├── commands/*.trigger       # fila de comandos da UI
└── logs/
    └── <app_name>/
        ├── history.jsonl                      # 1 linha JSON por execução
        └── <YYYYMMDD_HHMMSS>_<exec_id>.log    # stdout+stderr capturados
```

### config.yaml vs config.example.yaml

- `config.yaml` **não é versionado** (caminhos absolutos, apps por ambiente)
- `config.example.yaml` **é versionado** (template com estrutura + comentários)
- No primeiro boot, `_ensure_config_exists` copia example → config

### state.tmp com sufixo único

```python
tmp = STATE_FILE.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
```

Evita race condition quando múltiplas threads/processos chamam `save_state`
concorrentemente.

---

## Hot reload

```python
def _check_config_changed(self) -> bool:
    current_mtime = self._config_path.stat().st_mtime
    if current_mtime > self._last_config_mtime:
        self._last_config_mtime = current_mtime
        return True
    return False
```

A cada 5s, `_monitor_loop` verifica o mtime do `config.yaml`. Se mudou,
`_reload_config` calcula o diff:

- **Apps novos** → criados no state
- **Apps removidos** → disabled + removidos do state
- **Apps alterados** (cmd/cwd/slot/schedule/pause_between/ram/timeout/
  pre_start/pre_start_timeout/pre_start_required)
  → disabled + re-enabled com nova config
- **Apps iguais** → não são tocados (zero downtime)

Você pode editar o `config.yaml` manualmente ou via UI; as duas formas
disparam reload em até 5s.

---

## Logs e observabilidade

### Por execução

```
logs/meu_robo/
├── 20260423_143027_a1b2c3d4.log     # stdout+stderr prefixado [out]/[err]
├── 20260423_153012_e5f6g7h8.log
└── history.jsonl                     # metadados estruturados
```

Cada linha do `history.jsonl`:

```json
{
  "exec_id": "a1b2c3d4",
  "app_name": "meu_robo",
  "started_at": 1776909348.03,
  "finished_at": 1776909478.26,
  "duration_s": 130.23,
  "exit_code": 0,
  "status": "done",
  "error": "",
  "pid": 11140,
  "log_file": "C:/.../20260423_143027_a1b2c3d4.log",
  "peak_ram_mb": 847.2
}
```

Mantém as últimas 500 execuções (trim automático).

### Stream em tempo real

Cada linha do stdout/stderr é escrita imediatamente no `.log`:

```python
async def _stream_output(self, stream, prefix: bytes):
    while True:
        line = await stream.readline()
        if not line:
            break
        self._exec_logger.write(prefix + line)
```

A aba **📺 Ao vivo** do dashboard lê os últimos 64 KB do log com
refresh de 5s via `st.fragment`.

### Alertas Telegram

Disparados automaticamente (se `.env` configurado):

- ❌ App falhou (exit != 0) + últimas linhas do stderr
- ⏰ App excedeu timeout
- 🔺 App excedeu `max_ram_mb`
- 💥 Serviço `always` crashou e foi reiniciado

### Integração com `telemonit`

O `TelegramAlerter` é um wrapper async sobre a lib externa
[`telemonit`](https://github.com/marceloFilho-hub/telemonit) — toda a
notificação real (Telegram + JSONL no Drive) acontece lá. Vantagens:

- **Audit trail automático**: cada falha/timeout/RAM/restart vira uma linha
  JSONL em `eventos_control_panel_<YYYY-MM>.jsonl` na pasta de logs do Drive,
  com `run_id` correlacionável aos arquivos `.log` locais.
- **`run_id` por execução**: gerado no `ProcessManager.start()` no formato
  `<app>-<YYYYmmdd-HHMMSS>` e propagado a todos os alertas; aparece no
  cabeçalho da mensagem do Telegram e como campo first-class no evento JSONL.
- **Throttle de storm protection** já vem da `telemonit` (alerta repetido
  da mesma run em <5min é engolido).
- **Resolução `drive:<file_id>`** para credenciais: zero secrets em git.

#### Variáveis de ambiente esperadas (lidas pela `telemonit`)

```ini
MONITOR_PROJETO=control_panel             # opcional — default: control_panel
MONITOR_TG_TOKEN=drive:<file_id>          # ou token cru
MONITOR_TG_CHAT_ID=drive:<file_id>        # ou chat_id cru
MONITOR_DRIVE_LOG_FOLDER=<id_da_pasta>
MONITOR_NIVEL=alerta                      # info | alerta | erro
GOOGLE_APPLICATION_CREDENTIALS=credentials/sa.json
```

> Se o `config.yaml` tiver `alerts.telegram_bot_token` / `telegram_chat_id`
> preenchidos, eles têm precedência sobre as env vars (passados via
> `telemonit.configurar` no `__init__` do alerter).

---

## API interna (comandos)

Controle do orquestrador via arquivos `.trigger` (pattern atômico para
IPC entre dashboard e orchestrator):

```
commands/
├── start_<app>.trigger      # ativar app
├── stop_<app>.trigger       # desativar + matar
├── pause_<app>.trigger      # suspender sem matar
├── resume_<app>.trigger     # retomar do pause
├── start_all.trigger        # ativar todos
├── stop_all.trigger         # desativar todos
├── reload.trigger           # forçar reload do config.yaml
└── shutdown.trigger         # encerrar orquestrador (mata todos os apps
                             # e seus subprocessos; preserva config.yaml)
```

O `_monitor_loop` lê e remove os triggers a cada 5s. O dashboard usa
`write_command(COMMANDS_DIR, action, app_name)` para gerar os arquivos.

### Botão "Derrubar" (shutdown via UI)

A aba **Status** tem um botão **⏻ Derrubar** ao lado de Start All / Stop All.
Ao clicar, aparece confirmação inline; confirmando, o orquestrador:

1. Recebe o trigger `shutdown` em até 5s
2. Chama `_stop_all()` — mata todos os apps via Job Object + cleanup
3. Sai do `_monitor_loop` e faz `await stop()` (cleanup idempotente)
4. O entrypoint em `main.py` derruba o dashboard Streamlit no `finally`

**O `config.yaml` é preservado intacto** — nenhuma rota de shutdown o
modifica. Para reiniciar, use o atalho do desktop ou `python -m src.main`.

### Botão "🧹 Limpar VM"

Ao lado do Derrubar, dispara `full_cleanup()` síncrono no contexto do
Streamlit (não passa por arquivo `.trigger`, é chamada direta). Mata
órfãos, purga `__pycache__` de todos os apps, limpa `%TEMP%` e Lixeira.
Detalhes na seção [Cleanup pós-execução e da VM](#cleanup-pós-execução-e-da-vm).

---

## Estrutura do projeto

Organização por **domínio** (boas práticas open source). Cada subpacote
tem uma única responsabilidade e expõe sua API via `__init__.py`.

```
control_panel/
├── .streamlit/config.toml       # tema light do Streamlit
├── .claude/settings.json        # permissões do Claude Code
├── scripts/                     # instalação e launcher
│   ├── install.bat              # cria venv + deps + atalho desktop
│   ├── iniciar_painel.vbs       # launcher silencioso (alvo do atalho)
│   └── desinstalar.bat          # remove atalho + mata processo
├── src/
│   ├── __init__.py
│   ├── main.py                  # ⭐ ENTRY POINT (único arquivo na raiz)
│   │
│   ├── config/                  # 📝 leitura e escrita do config.yaml
│   │   ├── loader.py            #   parse + dataclasses (AppConfig etc.)
│   │   └── writer.py            #   CRUD atômico com tmp+rename
│   │
│   ├── orchestration/           # 🎯 core — scheduler, filas, state
│   │   ├── orchestrator.py      #   semáforos + memory gate + hot reload
│   │   └── state.py             #   ControlPlaneState + Command + IPC
│   │
│   ├── process/                 # ⚙️ ciclo de vida dos subprocessos
│   │   ├── manager.py           #   subprocess + monitoramento + cleanup
│   │   ├── python_runner.py     #   detecta .venv/.env de projetos Python
│   │   ├── windows_job.py       #   wrapper Windows Job Objects (pywin32)
│   │   ├── resource_monitor.py  #   psutil (RAM/CPU/kill_tree)
│   │   ├── git_updater.py       #   auto-update git pré-rodada
│   │   └── cleanup.py           #   per_app_cleanup + full_cleanup (VM)
│   │
│   ├── observability/           # 👁 logs e alertas
│   │   ├── logger.py            #   ExecutionLogger + history.jsonl
│   │   └── alerter.py           #   Telegram Bot API (httpx async)
│   │
│   └── ui/                      # 🖥️ interface web
│       └── dashboard.py         #   Streamlit (5 abas + fragments)
│
├── logs/                        # logs persistentes (gitignore)
│   ├── hidra_control.log        # log geral (rotativo)
│   └── {app_name}/              # por app
│       ├── history.jsonl
│       └── {timestamp}_{id}.log
│
├── commands/                    # IPC dashboard → orchestrator
│   └── *.trigger                # arquivos atômicos (gitignore)
│
├── config.example.yaml          # template versionado
├── config.yaml                  # específico da máquina (gitignore)
├── pyproject.toml               # deps + entry point
└── README.md                    # este arquivo
```

### Regras de dependência entre subpacotes

```
ui ──────▶ config, orchestration, process, observability
orchestration ─▶ config, process, observability
process ─────▶ config, observability
observability ▶ (sem deps internos)
config ──────▶ (sem deps internos)
```

Sem ciclos. `observability` e `config` são as folhas — módulos puros
sem dependências entre pacotes internos.

---

## Configurações avançadas

### settings globais (`config.yaml`)

```yaml
settings:
  heavy_slots: 1                # max heavy paralelos (aumentar se VM aguenta)
  light_slots: 3                # max light paralelos
  ram_safety_margin_mb: 512     # RAM reservada ao SO e ao orquestrador
  log_dir: logs/
  log_rotation: "10 MB"         # rotação do log geral
  log_retention: 30             # dias de retenção
```

### Capacity planning

Regra empírica para `ram_safety_margin_mb`:

```
safety = SO_overhead + orchestrator_overhead + buffer
       = 512 + 80 + 500
       = ~1100 MB em VMs com < 8 GB
       = 512 MB em VMs com >= 16 GB
```

### Tuning de slots

```
heavy_slots = (RAM_total_GB - 2) / max_ram_heavy_GB
light_slots = (CPU_count × 2) limitado pela soma de max_ram_light
```

Exemplo VM 16 GB / 4 vCPU:
- `heavy_slots = (16 − 2) / 2 = 7` → mas limitar em 2-3 pra deixar folga
- `light_slots = 4 × 2 = 8` → limitar em 5 pra conviver com heavy

---

## Deploy em VM

### 1. Primeira vez na VM

```bash
git clone git@github.com:org/control-panel.git
cd control-panel
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

### 2. Configurar `.env` (Telegram)

```bash
copy .env.example .env
notepad .env  # preencher tokens
```

### 3. Primeiro boot

```bash
python -m src.main
```

`config.yaml` é criado a partir do template. Acesse `http://localhost:9000`
e cadastre os apps de produção pela UI.

### 4. Rodar como serviço / auto-start

Opções (escolher uma):

**A. Windows Task Scheduler** (recomendado)
- Gatilho: "At log on" ou "At system startup"
- Ação: `C:\caminho\.venv\Scripts\python.exe -m src.main`
- Working directory: `C:\caminho\control_panel`

**B. NSSM (Non-Sucking Service Manager)**
```bash
nssm install HidraControlPlane "C:\caminho\.venv\Scripts\python.exe" "-m src.main"
nssm set HidraControlPlane AppDirectory "C:\caminho\control_panel"
nssm start HidraControlPlane
```

> ⚠️ Se rodar como serviço (Session 0), apps com `gui: true` **não**
> conseguirão exibir janelas. Use Task Scheduler com "Run only when user
> is logged on" para apps GUI.

### 5. GitHub Actions para deploy contínuo

O projeto tem `.github/workflows/deploy-vm.yml`. Configure um self-hosted
runner na VM e o push em `master` dispara:

```yaml
- name: Atualizar código
  shell: powershell
  run: |
    cd C:\caminho\control_panel
    git fetch origin
    git reset --hard origin/master
    # Control Panel detecta config.yaml via mtime — apps não reiniciam
```

---

## Troubleshooting

### App não aparece na lista

- Verifique se o `config.yaml` está correto: `python -c "import yaml; print(yaml.safe_load(open('config.yaml')))"`
- Veja o log: `logs/hidra_control.log`
- Force reload: crie `commands/reload.trigger`

### Janela GUI não abre

- Marque `gui: true` no app
- Se rodando como serviço em Session 0: migrar para Task Scheduler
- Verifique se o app usa `pythonw.exe` (não `python.exe` que bloqueia)
- Execute manualmente pra validar: `.venv\Scripts\pythonw.exe seu_main.py`

### `[WinError 5] Acesso negado`

Subprocess não conseguiu lançar. Causas:
- Caminho com aspas literais (verifique quoting em `cmd`)
- Antivírus bloqueando (adicione pasta como exceção)
- Executável não tem permissão de execução (right-click → Properties → Unblock)

### NotFoundError: removeChild no dashboard

Erro do React do Streamlit. Mitigações já aplicadas:
- `st.fragment(run_every=N)` isola abas dinâmicas
- Botões sempre renderizados (variando `disabled`)
- Sem `time.sleep() + st.rerun()` manuais

Se persistir: reduza `run_every` para 5 em `_status_fragment`.

### Memória não libera após kill

- Confirme que `_finalize()` foi chamado (veja log "Cleanup: N proc(s)")
- Verifique apps `gui: true`: eles não usam Job Object, cleanup via psutil
- Failsafe: `taskkill /F /T /PID <pid>` manualmente

### VM enche de lixo após dias de uso

Sintoma: disco livre caindo de ~10 GB para ~1 GB em 24h, RAM com muito
"em uso" mesmo sem apps ativos. Causas comuns:

- **Drivers Selenium/Playwright órfãos** (chromedriver, geckodriver,
  msedgedriver, playwright-headless-shell): escapam do Job Object quando
  o app spawna o driver via biblioteca que dispara `CREATE_BREAKAWAY_FROM_JOB`.
  O `per_app_cleanup` já mata os nomes padrão após cada rodada — se seu
  app usa um driver/extensão custom, adicione em `kill_orphans` no config.
- **`__pycache__` acumulado** em repos atualizados via auto-update:
  cada `git reset --hard` invalida bytecode antigo mas não apaga. Resolvido
  pelo `purge_pycache` em `per_app_cleanup`.
- **`%TEMP%` lotado**: apps que baixam planilhas/PDFs e nunca limpam.
  Use o botão **🧹 Limpar VM** na aba Status (só remove arquivos com
  `mtime > 5 min`, então é seguro mesmo com apps rodando).
- **Lixeira do Windows cheia**: alguns apps usam `send2trash`. O botão
  Limpar VM esvazia via `Clear-RecycleBin -Force`.

### App falha com "Hook pré-execução: ..."

Algum comando do `pre_start` saiu com exit code != 0 (ou o `pre_start_timeout`
estourou). Diagnóstico:

- Abra o `.log` da execução em `logs/<app>/<timestamp>_<id>.log` — as linhas
  prefixadas com `[pre]` mostram a saída exata de cada hook.
- Se for um problema temporário (lock do `pip`, indisponibilidade de rede),
  considere `pre_start_required: false` — falha apenas loga e o app segue.
- Se `{python}`/`{pip}` resolveram para o Python do PATH em vez do `.venv`:
  confirme que existe `cwd/.venv/Scripts/python.exe` no working directory
  do app (não na raiz do control_panel).

### App falha com "auto-update: ..."

O `git fetch` ou `git reset --hard` falhou (rede, auth, repo corrompido).
O ciclo é abortado **sem iniciar o processo** (com `on_failure: abort_cycle`,
o default) e o próximo ciclo tenta de novo. Diagnóstico:

- O log do orchestrator (`logs/orchestrator-*.log`) mostra a saída
  exata do git em `auto-update FALHOU: <stderr>`.
- Se o repo está em estado inválido (rebase/merge no meio), resolver
  manualmente no `cwd` e o próximo ciclo recupera.
- Para repos que **não devem** ser atualizados (ex: desenvolvimento
  ativo na VM), adicionar o repo_root absoluto em
  `settings.auto_update.skip_paths`.
- Para tolerar falhas temporárias sem abortar, mudar
  `settings.auto_update.on_failure` para `skip_update` — o app roda
  com a versão em disco e segue (warning no log).

### Apps sumiram após git pull

- `config.yaml` está no `.gitignore` por design (per-machine)
- Se perdeu, restaure do backup: `config.yaml.bak-*`
- Se não tem backup: recadastre pela UI

---

## Limitações conhecidas

- **Apenas Windows** por enquanto (Job Objects, pywin32, pythonw.exe). Portar
  para Linux requer `prctl(PR_SET_PDEATHSIG)` ou cgroups.
- **Session 0 isolation**: apps GUI precisam rodar na sessão do usuário logado.
- **Sem HA**: é um único processo orquestrador. Para alta disponibilidade,
  use 2 VMs com apps disjuntos.
- **Dashboard single-user**: Streamlit sem autenticação. Em produção,
  coloque atrás de Nginx/Cloudflare Access ou VPN.
- **Sem dependências entre apps**: ainda não tem DAG. Se `job_B` precisa
  do `job_A`, use `pause_between` generoso ou acione `job_B` no fim do `job_A`.

---

## Roadmap

- [ ] Dependências entre apps (grafo DAG simples)
- [ ] Autenticação no dashboard (token/OAuth)
- [ ] Suporte a Docker para apps Linux-only
- [ ] Métricas Prometheus `/metrics`
- [ ] Retry com backoff exponencial configurável
- [ ] UI de calendário para visualizar próximas execuções

---

## Licença

Proprietário. Todos os direitos reservados.

---

**Autor**: Marcelo Leandro dos Santos Filho
**Repositório**: https://github.com/marceloFilho-hub/control-panel
