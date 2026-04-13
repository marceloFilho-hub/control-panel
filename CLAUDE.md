# Control Panel

Orquestrador central Python para gerenciar múltiplas automações numa única VM.
Projeto genérico e reutilizável — não pertence a nenhum monorepo específico.

## Identificação

- **Repo GitHub:** `marceloFilho-hub/control-panel` (privado)
- **Branch padrão:** `master`

## Arquitetura

- **main.py** — entry point, inicia orchestrator + dashboard
- **orchestrator.py** — core: APScheduler + Semaphore slots + subprocess
- **process_manager.py** — lança, monitora e mata processos
- **resource_monitor.py** — psutil: RAM/CPU por PID + kill tree
- **alerter.py** — notificações Telegram (falha, timeout, RAM, crash)
- **config_loader.py** — parse YAML + substituição de `${ENV_VARS}`
- **state.py** — estado compartilhado via JSON (orchestrator ↔ dashboard)
- **dashboard.py** — Streamlit UI dark mode (porta 9000)

## Regras

- Cada app roda no seu próprio `.venv` (subprocess isolado)
- Slot **HEAVY**: max 1 simultâneo (`Semaphore(1)`) — jobs pesados ou COM/GUI
- Slot **LIGHT**: max 3 simultâneos (`Semaphore(3)`) — jobs rápidos e leves
- Slot **ALWAYS**: serviços permanentes com auto-restart
- Comunicação orchestrator↔dashboard via `state.json` + `commands/*.trigger`
- O `config.yaml` é o único arquivo que precisa ser editado para adicionar/remover apps
- Overhead total do orquestrador: ~80 MB RAM

## Decisão arquitetural

Docker foi descartado para VMs Windows com 16GB porque:
1. WSL2 overhead de 1.5-2.5 GB RAM fixo
2. COM/pywinauto incompatíveis com containers Linux
3. Híbrido Docker+nativo = pior dos dois mundos
4. Apps Python com `.venv` já são isoladas
