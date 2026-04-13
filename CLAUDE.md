# Hidra Control Plane

Orquestrador central Python para todas as automações da pasta `automacoes/`.

## Arquitetura

- **main.py** — entry point, inicia orchestrator + dashboard
- **orchestrator.py** — core: APScheduler + Semaphore slots + subprocess
- **process_manager.py** — lança, monitora e mata processos
- **resource_monitor.py** — psutil: RAM/CPU por PID
- **alerter.py** — notificações Telegram
- **config_loader.py** — parse YAML + substituição de env vars
- **state.py** — estado compartilhado via JSON (orchestrator ↔ dashboard)
- **dashboard.py** — Streamlit UI (porta 9000)

## Regras

- Cada app roda no seu próprio `.venv` (subprocess isolado)
- Slot HEAVY: max 1 simultâneo (Semaphore(1))
- Slot LIGHT: max 3 simultâneos (Semaphore(3))
- Slot ALWAYS: serviços permanentes com auto-restart
- Comunicação orchestrator↔dashboard via `state.json` + `commands/*.trigger`
