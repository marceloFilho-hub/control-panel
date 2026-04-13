# Control Panel

Orquestrador central Python para gerenciar a execução de múltiplas automações numa única VM Windows, sem Docker.

## Por que existe

Numa VM com 16 GB de RAM e 4 vCPUs rodando ~16 automações Python, Docker Desktop + WSL2 consumiria 1.5–2.5 GB apenas de overhead. Algumas apps requerem COM automation (pywin32) ou GUI nativa (pywinauto), tornando containers Linux inviáveis. O Control Panel resolve o mesmo problema com **~80 MB de overhead** e zero dependências externas.

## Como funciona

```
┌──────────────────────────────────────────────────────┐
│                    Control Panel                      │
│                                                       │
│  ┌───────────┐   ┌──────────┐   ┌────────────────┐  │
│  │ Scheduler │ → │  Queue   │ → │ Process Manager │  │
│  │(APSched)  │   │(Semaphore│   │ (subprocess +   │  │
│  │cron/inter │   │ heavy=1  │   │  psutil monitor)│  │
│  │val/manual │   │ light=3) │   │                 │  │
│  └───────────┘   └──────────┘   └────────────────┘  │
│        │                               │              │
│        ▼                               ▼              │
│  ┌──────────┐                  ┌──────────────────┐  │
│  │ Alerter  │                  │ Resource Monitor  │  │
│  │(Telegram)│                  │ RAM/CPU por PID   │  │
│  └──────────┘                  └──────────────────┘  │
│                                                       │
│  ┌────────────────────────────────────────────────┐  │
│  │           Dashboard (Streamlit :9000)           │  │
│  │  KPIs · Slots · Status por app · Run manual    │  │
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

Toda a configuração fica no `config.yaml`. Exemplo de uma app:

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
    restart_on_crash: false  # true para serviços always-on
```

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

# Dashboard fica disponível em http://localhost:9000
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

### Trigger manual (via dashboard ou arquivo)

Pelo dashboard: botão **Run** ao lado de cada app.

Via arquivo (para scripts externos):
```bash
# Cria um trigger para rodar a app
echo. > commands/run_minha_app.trigger
```

## Dashboard

O dashboard Streamlit roda na porta 9000 com dark mode:

- **KPIs**: total de apps, rodando, na fila, falhas, RAM/CPU da VM
- **Slots**: barras de progresso heavy/light
- **Tabela de apps**: status, RAM, CPU, hora, próximo run, botão Run
- **Histórico**: últimas 10 execuções com duração e erros

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
    ├── orchestrator.py    # Core: scheduler + semáforos + ciclo de vida
    ├── process_manager.py # Subprocess: start, monitor, kill
    ├── resource_monitor.py# psutil: métricas por PID
    ├── alerter.py         # Telegram: falha, timeout, RAM, crash
    ├── state.py           # Estado compartilhado via JSON
    ├── config_loader.py   # Parse YAML + ${ENV_VARS}
    └── dashboard.py       # Streamlit UI dark mode (:9000)
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

## Roadmap

- [ ] Endpoint HTTP no orchestrator (substituir IPC por arquivo)
- [ ] Página de logs no dashboard (tail -f por app)
- [ ] Gráfico de RAM/CPU histórico (últimas 24h)
- [ ] Suporte a dependências entre apps (app B só roda após app A)
- [ ] Health check HTTP para serviços always-on (além de PID alive)
- [ ] Export de métricas para Prometheus (opcional)
