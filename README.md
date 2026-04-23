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
- [Orquestração e filas](#orquestração-e-filas)
- [Cleanup e Job Objects](#cleanup-e-job-objects)
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
    5c. ProcessManager.start()
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

### Instalação

```bash
git clone https://github.com/marceloFilho-hub/control-panel.git
cd control-panel
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

### Primeira execução

```bash
python -m src.main
```

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
- **Apps alterados** (cmd/cwd/slot/schedule/pause_between/ram/timeout)
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
└── reload.trigger           # forçar reload do config.yaml
```

O `_monitor_loop` lê e remove os triggers a cada 5s. O dashboard usa
`write_command(COMMANDS_DIR, action, app_name)` para gerar os arquivos.

---

## Estrutura do projeto

```
control_panel/
├── .streamlit/
│   └── config.toml              # tema light do Streamlit
├── .claude/
│   └── settings.json            # permissões do Claude Code
├── src/
│   ├── main.py                  # entry point (asyncio + dashboard)
│   ├── orchestrator.py          # core — scheduler + semáforos + memory gate
│   ├── process_manager.py       # lifecycle por processo + monitoramento
│   ├── windows_job.py           # wrapper de Job Objects
│   ├── python_app_runner.py     # detect .venv/.env + build_command
│   ├── executable_detector.py   # heurísticas para .exe/.bat/.ps1/.lnk
│   ├── config_loader.py         # parse do config.yaml + dataclasses
│   ├── config_writer.py         # CRUD do config.yaml
│   ├── execution_logger.py      # history.jsonl por app + logs individuais
│   ├── resource_monitor.py      # psutil wrappers (RAM/CPU/kill_tree)
│   ├── state.py                 # ControlPlaneState + Command + serialization
│   ├── alerter.py               # Telegram Bot API (httpx async)
│   └── dashboard.py             # Streamlit app (5 abas + fragments)
├── config.example.yaml          # template
├── pyproject.toml               # deps + entry point
└── README.md                    # este arquivo
```

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
