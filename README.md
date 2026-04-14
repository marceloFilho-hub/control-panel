# Control Panel

Orquestrador central Python para gerenciar a execução de múltiplas automações numa única VM Windows, sem Docker.

## Por que existe

Numa VM com 16 GB de RAM e 4 vCPUs rodando ~16 automações Python, Docker Desktop + WSL2 consumiria 1.5–2.5 GB apenas de overhead. Algumas apps requerem COM automation (pywin32) ou GUI nativa (pywinauto), tornando containers Linux inviáveis. O Control Panel resolve o mesmo problema com **~80 MB de overhead** e zero dependências externas.

## Como funciona

O orquestrador inicia em **modo IDLE** — nenhum app roda automaticamente, exceto os marcados com `auto_start: true` no `config.yaml`. O controle total e feito pela interface web (dashboard) na porta 9000.

```
┌──────────────────────────────────────────────────────┐
│              Control Panel (modo IDLE)                 │
│                                                       │
│  ┌───────────┐   ┌──────────┐   ┌────────────────┐  │
│  │ Scheduler │ → │  Queue   │ → │ Process Manager │  │
│  │(APSched)  │   │(Semaphore│   │ (subprocess +   │  │
│  │cron/inter │   │ heavy=1  │   │  psutil monitor)│  │
│  │val/manual │   │ light=3) │   │                 │  │
│  └───────────┘   └──────────┘   └────────────────┘  │
│        │              ▲                │              │
│        ▼              │                ▼              │
│  ┌──────────┐   ┌──────────┐   ┌──────────────────┐ │
│  │ Alerter  │   │ Commands │   │ Resource Monitor  │ │
│  │(Telegram)│   │(UI→.trig │   │ RAM/CPU por PID   │ │
│  └──────────┘   │ger files)│   └──────────────────┘ │
│                  └──────────┘                         │
│  ┌────────────────────────────────────────────────┐  │
│  │           Dashboard (Streamlit :9000)           │  │
│  │  KPIs · Slots · Start/Stop/Pause por app       │  │
│  │  Start All · Stop All · Historico               │  │
│  └────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────┘
```

## Sistema de Slots

O mecanismo anti-colisão usa semáforos para controlar execução simultânea:

| Slot | Concorrência | Uso |
|---|---|---|
| **heavy** | 1 por vez | Jobs pesados, COM/Excel, browser automation |
| **light** | Até 3 paralelos | Jobs rápidos e leves (<512 MB) |
| **always** | Sem limite | Serviços web permanentes com auto-restart |

Quando um job `heavy` está rodando, qualquer outro job `heavy` fica na fila (`queued`) até o slot liberar.

## Instalação

```bash
# Clonar
git clone https://github.com/marceloFilho-hub/control-panel.git
cd control-panel

# Criar ambiente
python -m venv .venv
.venv\Scripts\activate

# Instalar dependências
pip install -e .

# Configurar
copy .env.example .env
# Editar .env com tokens Telegram
# Editar config.yaml com os paths das suas apps
```

### Dependências

| Pacote | Versão | Função |
|---|---|---|
| `apscheduler` | >=3.10 | Scheduler cron/interval |
| `psutil` | >=5.9 | Monitor RAM/CPU, kill process tree |
| `pyyaml` | >=6.0 | Parse config.yaml |
| `httpx` | >=0.27 | Telegram API (async) |
| `loguru` | >=0.7 | Logging com rotação |
| `streamlit` | >=1.35 | Dashboard web |
| `python-dotenv` | >=1.0 | Variáveis de ambiente |

## Configuração

Toda a configuracao fica no `config.yaml`. Exemplo de uma app:

```yaml
apps:
  minha_app:
    slot: heavy              # heavy | light | always
    cwd: "C:/caminho/da/app"
    cmd: ".venv/Scripts/python main.py"
    schedule: "cron(hour=7, minute=0)"   # ou interval(), loop, manual
    max_ram_mb: 1024
    timeout: 600             # segundos
    priority: 1              # menor = maior prioridade na fila
    restart_on_crash: false  # true para servicos always-on
    auto_start: false        # true = inicia ao subir o orchestrator (padrao: false)
```

O campo `auto_start` controla se a app inicia automaticamente quando o orquestrador sobe. Por padrao e `false` — a app fica em modo IDLE ate ser ativada manualmente pelo dashboard.

### Tipos de schedule

| Formato | Exemplo | Comportamento |
|---|---|---|
| `"manual"` | — | Só roda via dashboard ou trigger |
| `"loop"` | `pause_between: 600` | Roda continuamente com pausa entre ciclos |
| `"cron(...)"` | `"cron(hour=7, minute=30)"` | Horário fixo (APScheduler CronTrigger) |
| `"interval(...)"` | `"interval(minutes=15)"` | A cada N minutos/segundos |

### Variáveis de ambiente

```bash
# .env
TELEGRAM_BOT_TOKEN=123456:ABC...    # Bot para alertas
TELEGRAM_CHAT_ID=987654321          # Chat ID autorizado
PYTHONUTF8=1                        # Encoding para subprocessos
```

Variáveis no `config.yaml` são resolvidas automaticamente: `"${TELEGRAM_BOT_TOKEN}"` → valor do .env.

## Uso

```bash
# Iniciar orquestrador + dashboard
.venv\Scripts\python -m src.main

# O orquestrador inicia em modo IDLE (nenhum app roda automaticamente)
# Use o dashboard em http://localhost:9000 para ativar apps
```

### Rodar como serviço Windows

```bash
# Opção 1: nssm (recomendado)
nssm install ControlPanel "C:\...\control_panel\.venv\Scripts\python.exe" "-m" "src.main"
nssm set ControlPanel AppDirectory "C:\...\control_panel"
nssm start ControlPanel

# Opção 2: Task Scheduler
# Ação: .venv\Scripts\python.exe -m src.main
# Trigger: At startup
# Configurações: Restart on failure
```

### Controle via dashboard ou arquivo trigger

Pelo dashboard: botoes **Start**, **Pause** e **Stop** ao lado de cada app, alem de **Start All** e **Stop All** globais.

Via arquivo (para scripts externos):
```bash
# Comandos disponiveis via arquivo .trigger
echo. > commands/start_minha_app.trigger    # Ativa e inicia a app
echo. > commands/stop_minha_app.trigger     # Para e desativa a app
echo. > commands/pause_minha_app.trigger    # Pausa (impede novas execucoes)
echo. > commands/resume_minha_app.trigger   # Retoma app pausada

# Comandos globais
echo. > commands/start_all.trigger          # Ativa todos os apps
echo. > commands/stop_all.trigger           # Para todos os apps
```

## Dashboard

O dashboard Streamlit roda na porta 9000 com dark mode:

- **KPIs**: Apps, Ativas, Rodando, Pausadas, Falhas, RAM/CPU da VM
- **Controles globais**: Start All / Stop All
- **Slots**: barras de progresso heavy/light
- **Tabela de apps**: status, indicador enabled/disabled, RAM, CPU, hora, proximo run, botoes Start/Pause/Stop por app
- **Historico**: ultimas 10 execucoes com duracao e erros

Estados possiveis de cada app: `off`, `queued`, `running`, `done`, `failed`, `timeout`, `paused`.

## Monitoramento e Alertas

O orquestrador monitora cada processo a cada 5 segundos:

| Evento | Ação |
|---|---|
| **Processo falhou** (exit != 0) | Log + alerta Telegram com stderr |
| **Timeout** excedido | Kill process tree + alerta Telegram |
| **RAM** acima do limite | Kill process tree + alerta Telegram |
| **Serviço always crashou** | Restart automático + alerta Telegram |

## Arquitetura de Arquivos

```
control_panel/
├── config.yaml            # Definição de todas as apps
├── pyproject.toml         # Dependências do projeto
├── .env                   # Tokens Telegram (não versionado)
├── .env.example
├── state.json             # Estado atual (gerado automaticamente)
├── commands/              # Triggers manuais (arquivos .trigger)
├── logs/
│   └── hidra_control.log  # Log rotativo (10 MB, 30 dias)
└── src/
    ├── main.py            # Entry point
    ├── orchestrator.py    # Core: modo IDLE + scheduler + semaforos + start/stop/pause
    ├── process_manager.py # Subprocess: start, monitor, kill
    ├── resource_monitor.py# psutil: métricas por PID
    ├── alerter.py         # Telegram: falha, timeout, RAM, crash
    ├── state.py           # Estado compartilhado via JSON + sistema de comandos
    ├── config_loader.py   # Parse YAML + ${ENV_VARS}
    └── dashboard.py       # Streamlit UI dark mode (:9000) com controles por app
```

## Comparação com Docker

| Aspecto | Docker Compose | Control Panel |
|---|---|---|
| Overhead RAM | 1.5–2.5 GB (WSL2) | ~80 MB |
| COM/pywinauto | Incompatível | Funciona |
| Curva de aprendizado | Docker + Compose + WSL2 | Python (já sabe) |
| Debug às 3h | 2 mundos (Docker + nativo) | 1 dashboard + 1 log |
| Dependência externa | Docker Desktop (licença) | Zero |
| Isolamento | Container | .venv por app (já existe) |

## Changelog

### 2026-04-13 — sem Jira
- feat: modo IDLE — orchestrator inicia sem rodar apps automaticamente
- feat: campo `auto_start` no config.yaml para apps que devem iniciar sozinhos
- feat: campo `enabled` no estado de cada app (ativado/desativado pela UI)
- feat: status `paused` e comandos start/stop/pause/resume/start_all/stop_all
- feat: botoes individuais Start/Pause/Stop por app no dashboard
- feat: botoes globais Start All / Stop All no dashboard
- feat: KPIs "Ativas" e "Pausadas" no dashboard
- chore: config.yaml limpo — removidos apps descontinuados

### 2026-04-12 — sem Jira
- feat: implementacao inicial do Hidra Control Plane

## Roadmap

- [ ] Endpoint HTTP no orchestrator (substituir IPC por arquivo)
- [ ] Pagina de logs no dashboard (tail -f por app)
- [ ] Grafico de RAM/CPU historico (ultimas 24h)
- [ ] Suporte a dependencias entre apps (app B so roda apos app A)
- [ ] Health check HTTP para servicos always-on (alem de PID alive)
- [ ] Export de metricas para Prometheus (opcional)
