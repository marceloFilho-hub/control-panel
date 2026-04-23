"""Dashboard Streamlit — painel administrativo do Hidra Control Plane."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import streamlit as st

from config_writer import delete_app, read_config_raw, upsert_app
from python_app_runner import build_command as py_build_command
from python_app_runner import detect_project
from execution_logger import (
    get_latest_log_path,
    list_apps_with_logs,
    read_history,
    read_log_content,
)
from state import AppState, ControlPlaneState, load_state, write_command

ROOT = Path(__file__).parent.parent
COMMANDS_DIR = ROOT / "commands"
CONFIG_PATH = ROOT / "config.yaml"
COMMANDS_DIR.mkdir(exist_ok=True)

# ── Cores do tema LIGHT ──────────────────────────────────────
BG_BASE = "#F8FAFC"     # fundo principal (branco suave)
BG_CARD = "#FFFFFF"     # cards
BG_CARD2 = "#F1F5F9"    # cabeçalhos/linhas alternadas
BORDER = "#E2E8F0"      # bordas
TEXT_MAIN = "#0F172A"   # texto principal (quase preto)
TEXT_MUTED = "#64748B"  # texto secundário (cinza médio)
TEXT_LABEL = "#475569"  # labels
SUCCESS = "#059669"     # verde (emerald 600)
ERROR = "#DC2626"       # vermelho (red 600)
WARNING = "#D97706"     # âmbar (amber 600)
INFO = "#2563EB"        # azul (blue 600)

STATUS_COLORS = {
    "running": SUCCESS,
    "done": SUCCESS,
    "off": TEXT_MUTED,
    "queued": INFO,
    "failed": ERROR,
    "timeout": WARNING,
    "paused": WARNING,
}

STATUS_ICONS = {
    "running": "●",
    "done": "✓",
    "off": "○",
    "queued": "⏳",
    "failed": "✗",
    "timeout": "⏰",
    "paused": "⏸",
}

SCHEDULE_LABELS = {
    "manual": "Manual (roda uma vez ao ativar)",
    "loop": "Repetir com tempo entre rodagens",
}


def send_command(action: str, app_name: str = "") -> None:
    write_command(COMMANDS_DIR, action, app_name)


def format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "—"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f}min"
    hours = minutes / 60
    return f"{hours:.1f}h"


def format_time(ts: float | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


# ═══════════════════════════════════════════════════════════════
# ABA: STATUS (dashboard operacional)
# ═══════════════════════════════════════════════════════════════

def render_kpi_row(state: ControlPlaneState) -> None:
    total = len(state.apps)
    running = sum(1 for a in state.apps.values() if a.status == "running")
    failed = sum(1 for a in state.apps.values() if a.status == "failed")
    enabled = sum(1 for a in state.apps.values() if a.enabled)
    waiting_mem = len(state.memory_queue)

    cols = st.columns(8)
    with cols[0]:
        st.metric("Apps", total)
    with cols[1]:
        st.metric("Ativas", enabled)
    with cols[2]:
        st.metric("Rodando", running)
    with cols[3]:
        st.metric("Fila Memória", waiting_mem)
    with cols[4]:
        st.metric("Falhas", failed)
    with cols[5]:
        st.metric("Mem. usada", f"{state.total_ram_mb / 1024:.1f} GB")
    with cols[6]:
        st.metric("Mem. livre", f"{state.available_ram_mb / 1024:.1f} GB")
    with cols[7]:
        st.metric("CPU VM", f"{state.total_cpu_pct:.0f}%")


def render_global_controls(state: ControlPlaneState) -> None:
    any_enabled = any(a.enabled for a in state.apps.values())
    all_enabled = all(a.enabled for a in state.apps.values()) if state.apps else False

    cols = st.columns([1, 1, 6])
    with cols[0]:
        if st.button("▶ Start All", type="primary", disabled=all_enabled, use_container_width=True):
            send_command("start_all")
            st.rerun()
    with cols[1]:
        if st.button("■ Stop All", type="secondary", disabled=not any_enabled, use_container_width=True):
            send_command("stop_all")
            st.rerun()


def render_slots(state: ControlPlaneState) -> None:
    c1, c2 = st.columns(2)
    with c1:
        used = state.heavy_slots_used
        total = state.heavy_slots_max
        st.markdown(f"**Slot Heavy:** {used}/{total}")
        st.progress((used / total) if total else 0)
    with c2:
        used = state.light_slots_used
        total = state.light_slots_max
        st.markdown(f"**Slot Light:** {used}/{total}")
        st.progress((used / total) if total else 0)


def render_app_table(state: ControlPlaneState) -> None:
    always_apps = {k: v for k, v in state.apps.items() if v.slot == "always"}
    heavy_apps = {k: v for k, v in state.apps.items() if v.slot == "heavy"}
    light_apps = {k: v for k, v in state.apps.items() if v.slot == "light"}

    for section_name, apps in [
        ("\U0001f504 Servicos Always-On", always_apps),
        ("\U0001f4aa Jobs Heavy (1 por vez)", heavy_apps),
        ("⚡ Jobs Light (ate 3 paralelos)", light_apps),
    ]:
        if not apps:
            continue
        st.markdown(f"### {section_name}")
        cols = st.columns([2.5, 1.2, 0.8, 0.8, 1.2, 1.2, 2.3])
        headers = ["App", "Status", "Memória", "CPU", "Hora", "Próximo", "Controles"]
        for col, header in zip(cols, headers):
            with col:
                st.caption(f"**{header}**")
        for name, app in sorted(apps.items(), key=lambda x: x[1].status != "running"):
            _render_app_row(name, app)


def _render_app_row(name: str, app: AppState) -> None:
    """Renderiza uma linha da tabela de apps com DOM estável e resistente
    a tradução automática do navegador."""
    icon = STATUS_ICONS.get(app.status, "?")
    color = STATUS_COLORS.get(app.status, TEXT_MUTED)

    # Sempre o MESMO valor pra célula Hora: pega o timestamp mais recente
    # (evita condicional que troca DOM e causa NotFoundError: removeChild)
    ts = app.started_at if app.status == "running" else app.finished_at
    hora = format_time(ts)
    ram = f"{app.ram_mb:.0f} MB" if app.ram_mb > 0 else "—"
    cpu = f"{app.cpu_pct:.0f}%" if app.cpu_pct > 0 else "—"
    next_run = app.next_run or "—"
    enabled_dot = (
        f"<span style='color:{SUCCESS}'>●</span>"
        if app.enabled
        else f"<span style='color:{TEXT_MUTED}'>○</span>"
    )

    cols = st.columns([2.5, 1.2, 0.8, 0.8, 1.2, 1.2, 2.3])

    # Todas as células usam markdown com translate="no" para evitar que
    # o tradutor do navegador mude conteúdo e confunda o React
    with cols[0]:
        st.markdown(
            f'{enabled_dot} <span style="color:{color};font-size:1.1em">{icon}</span> '
            f'<strong translate="no">{name}</strong>',
            unsafe_allow_html=True,
        )
    with cols[1]:
        st.markdown(
            f'<span class="notranslate" translate="no" style="color:{TEXT_MUTED};font-size:0.85em">{app.status.upper()}</span>',
            unsafe_allow_html=True,
        )
    with cols[2]:
        st.markdown(
            f'<span translate="no" style="color:{TEXT_MUTED};font-size:0.85em">{ram}</span>',
            unsafe_allow_html=True,
        )
    with cols[3]:
        st.markdown(
            f'<span translate="no" style="color:{TEXT_MUTED};font-size:0.85em">{cpu}</span>',
            unsafe_allow_html=True,
        )
    with cols[4]:
        st.markdown(
            f'<span translate="no" style="color:{TEXT_MUTED};font-size:0.85em">{hora}</span>',
            unsafe_allow_html=True,
        )
    with cols[5]:
        st.markdown(
            f'<span translate="no" style="color:{TEXT_MUTED};font-size:0.85em">{next_run}</span>',
            unsafe_allow_html=True,
        )
    with cols[6]:
        _render_app_controls(name, app)


def _render_app_controls(name: str, app: AppState) -> None:
    """Renderiza os 3 botões SEMPRE (apenas disabled se não aplicável)
    para manter a árvore DOM estável e evitar NotFoundError do React."""
    btn_cols = st.columns(3)
    is_running = app.status in ("running", "queued")
    is_paused = app.status == "paused"
    is_enabled = app.enabled

    can_start = (not is_enabled) or is_paused
    can_pause = is_enabled and not is_paused
    can_stop = is_enabled or is_running

    with btn_cols[0]:
        if st.button("▶", key=f"start_{name}", help="Iniciar", disabled=not can_start):
            send_command("start", name)
            st.rerun()
    with btn_cols[1]:
        if st.button("⏸", key=f"pause_{name}", help="Pausar", disabled=not can_pause):
            send_command("pause", name)
            st.rerun()
    with btn_cols[2]:
        if st.button("■", key=f"stop_{name}", help="Parar", disabled=not can_stop):
            send_command("stop", name)
            st.rerun()


# ═══════════════════════════════════════════════════════════════
# ABA: FILA
# ═══════════════════════════════════════════════════════════════

def render_queue_view(state: ControlPlaneState) -> None:
    st.markdown("### Filas de execução")

    # Banner de RAM disponível
    available_gb = state.available_ram_mb / 1024
    safety_mb = state.ram_safety_margin_mb
    usable_mb = max(0.0, state.available_ram_mb - safety_mb)
    st.markdown(
        f"**Memória disponível:** {available_gb:.2f} GB "
        f"(**{usable_mb:.0f} MB utilizáveis** após margem de {safety_mb} MB para o SO)"
    )

    # Filas por slot
    col1, col2 = st.columns(2)
    with col1:
        st.markdown(
            f'<strong translate="no">Slot Heavy</strong> '
            f'({state.heavy_slots_used}/{state.heavy_slots_max} em uso)',
            unsafe_allow_html=True,
        )
        if not state.heavy_queue:
            st.info("Nenhum app aguardando")
        else:
            for pos, app_name in enumerate(state.heavy_queue, 1):
                st.markdown(
                    f'`{pos}.` <strong translate="no">{app_name}</strong> — aguardando vaga',
                    unsafe_allow_html=True,
                )
    with col2:
        st.markdown(
            f'<strong translate="no">Slot Light</strong> '
            f'({state.light_slots_used}/{state.light_slots_max} em uso)',
            unsafe_allow_html=True,
        )
        if not state.light_queue:
            st.info("Nenhum app aguardando")
        else:
            for pos, app_name in enumerate(state.light_queue, 1):
                st.markdown(
                    f'`{pos}.` <strong translate="no">{app_name}</strong> — aguardando vaga',
                    unsafe_allow_html=True,
                )

    # Fila de memória
    st.divider()
    st.markdown("### Apps aguardando memória")
    st.caption(
        "Apps que já conquistaram o slot mas estão aguardando memória suficiente "
        "para iniciar com segurança."
    )
    if not state.memory_queue:
        st.info("Nenhum app aguardando memória")
    else:
        for pos, app_name in enumerate(state.memory_queue, 1):
            app = state.apps.get(app_name)
            next_info = f" — {app.next_run}" if app and app.next_run else ""
            st.markdown(
                f'<span style="color:{WARNING}">⏳</span> '
                f'`{pos}.` <strong translate="no">{app_name}</strong>{next_info}',
                unsafe_allow_html=True,
            )

    st.divider()
    st.markdown("### Apps rodando agora")
    running = [a for a in state.apps.values() if a.status == "running"]
    if not running:
        st.info("Nenhum app em execução")
    else:
        for app in running:
            st.markdown(
                f'<span style="color:{SUCCESS}">●</span> '
                f'<strong translate="no">{app.name}</strong> '
                f'— PID {app.pid} — {app.ram_mb:.0f} MB — '
                f'iniciado {format_time(app.started_at)}',
                unsafe_allow_html=True,
            )


# ═══════════════════════════════════════════════════════════════
# ABA: CONFIGURAR (CRUD de apps)
# ═══════════════════════════════════════════════════════════════

def render_config_tab(state: ControlPlaneState) -> None:
    # ── Seção 1: Cadastrar app Python ────────────────────────────────
    _render_python_registration(state)
    st.divider()

    # ── Seção 2: Apps cadastrados ────────────────────────────────────
    raw = read_config_raw(CONFIG_PATH)
    apps = raw.get("apps", {})

    if not apps:
        st.info(
            "Nenhum app cadastrado ainda. Use o formulário acima para "
            "apontar para o `main.py` do seu projeto."
        )
        return

    st.markdown("### \U0001f40d Apps cadastrados")
    for name, data in apps.items():
        _render_python_app_row(name, data, state)


def _render_python_registration(state: ControlPlaneState) -> None:
    """Seção para cadastrar um app Python apontando para o main.py.

    Detecta automaticamente o .venv e o .env do projeto. Modo direto,
    sem VBS/bat intermediários.
    """
    st.markdown("### \U0001f40d Cadastrar app Python")
    st.caption(
        "Cole o caminho do `main.py` (ou `monitor_ui.py`, etc.) e o Control "
        "Panel detecta automaticamente o `.venv` e o `.env` do projeto."
    )

    with st.expander("➕ Adicionar app Python (sem VBS/bat)", expanded=False):
        with st.form("py_new_app"):
            cols = st.columns([3, 1])
            with cols[0]:
                script_path = st.text_input(
                    "Caminho do script Python *",
                    placeholder="C:/Users/.../meu_projeto/src/main.py",
                    help="Cole o caminho completo do arquivo .py. O venv e o .env são descobertos automaticamente nas pastas pai.",
                )
            with cols[1]:
                app_name = st.text_input(
                    "Nome *",
                    placeholder="meu_app",
                    help="Identificador curto (sem espaços).",
                )

            # Preview da detecção
            if script_path.strip():
                try:
                    project = detect_project(script_path.strip())
                    c1, c2 = st.columns(2)
                    with c1:
                        if project.has_venv:
                            st.success(f"✅ venv detectado: `{project.python_exe}`")
                        else:
                            st.warning("⚠️ Sem .venv detectado — vai usar Python global")
                    with c2:
                        if project.env_file:
                            st.success(f"✅ .env detectado: `{project.env_file.name}`")
                        else:
                            st.info("ℹ️ Nenhum .env detectado (opcional)")
                except Exception as e:
                    st.error(f"Erro ao analisar path: {e}")

            st.markdown("**Configuração inicial**")
            c1, c2, c3, c4 = st.columns([1, 1, 1, 1])
            with c1:
                slot = st.selectbox("Slot", ["light", "heavy", "always"], index=0)
            with c2:
                ram = st.number_input("RAM máx (MB)", min_value=64, max_value=16384, value=512, step=64)
            with c3:
                args_input = st.text_input("Argumentos (opcional)", placeholder="--once --verbose")
            with c4:
                gui_chk = st.checkbox("📺 GUI", help="Marcar se o app abre janela (Tkinter, PyQt...)")

            submit = st.form_submit_button("➕ Cadastrar app", type="primary")

            if submit:
                if not script_path.strip() or not app_name.strip():
                    st.error("Preencha o caminho do script e o nome do app.")
                elif not Path(script_path.strip()).exists():
                    st.error(f"Arquivo não encontrado: {script_path}")
                else:
                    project = detect_project(script_path.strip())
                    cmd, cwd = py_build_command(project, args_input.strip(), gui=gui_chk)
                    app_data = {
                        "slot": slot,
                        "cwd": cwd,
                        "cmd": cmd,
                        "schedule": "manual",
                        "max_ram_mb": int(ram),
                        "timeout": 3600,
                        "_source": "python",
                    }
                    if project.env_file:
                        app_data["env_file"] = str(project.env_file).replace("\\", "/")
                    if gui_chk:
                        app_data["gui"] = True

                    upsert_app(CONFIG_PATH, app_name.strip(), app_data)
                    st.success(
                        f"✅ '{app_name}' cadastrado. Ajuste abaixo quando quiser "
                        f"(tempo entre rodagens, slot, etc.) ou clique em ▶ Start."
                    )
                    st.rerun()


def _render_python_app_row(name: str, data: dict, state: ControlPlaneState) -> None:
    """Card inline para app Python cadastrado (com opções editáveis)."""
    app_state = state.apps.get(name)

    # Status badge
    live_badge = ""
    if app_state:
        color = STATUS_COLORS.get(app_state.status, TEXT_MUTED)
        icon = STATUS_ICONS.get(app_state.status, "?")
        live_badge = (
            f"<span style='color:{color};font-size:1.1em'>{icon}</span> "
            f"<span style='color:{TEXT_MUTED}'>{app_state.status}</span>"
        )

    gui_flag = "📺 " if data.get("gui") else ""
    st.markdown(
        f"#### \U0001f40d {gui_flag}<strong translate='no'>{name}</strong> {live_badge}",
        unsafe_allow_html=True,
    )
    st.caption(f"Comando: `{data.get('cmd', '')}`")
    st.caption(f"cwd: `{data.get('cwd', '')}`" + (f" | env_file: `{data.get('env_file')}`" if data.get("env_file") else ""))

    current_pause = int(data.get("pause_between", 600))
    current_slot = data.get("slot", "light")
    current_ram = int(data.get("max_ram_mb", 512))
    is_loop = data.get("schedule") == "loop" or data.get("auto_start", False)

    c1, c2, c3, c4, c5, c6 = st.columns([1.2, 2, 1.2, 1.2, 1, 1])

    with c1:
        enabled_chk = st.checkbox(
            "Executar",
            value=is_loop,
            key=f"py_en_{name}",
        )
    with c2:
        if current_pause >= 3600 and current_pause % 3600 == 0:
            du, dv = "horas", current_pause // 3600
        elif current_pause >= 60 and current_pause % 60 == 0:
            du, dv = "minutos", current_pause // 60
        else:
            du, dv = "segundos", current_pause
        tc1, tc2 = st.columns([1, 1])
        with tc1:
            val = st.number_input(
                "Tempo entre rodagens",
                min_value=1, max_value=86400,
                value=int(dv),
                key=f"py_pv_{name}",
            )
        with tc2:
            unit = st.selectbox(
                "Un.",
                ["segundos", "minutos", "horas"],
                index=["segundos", "minutos", "horas"].index(du),
                key=f"py_pu_{name}",
            )
        pause_seconds = int(val) * {"segundos": 1, "minutos": 60, "horas": 3600}[unit]
    with c3:
        slot = st.selectbox(
            "Slot",
            ["heavy", "light", "always"],
            index=["heavy", "light", "always"].index(current_slot),
            key=f"py_slot_{name}",
        )
    with c4:
        ram = st.number_input(
            "RAM máx (MB)",
            min_value=64, max_value=16384,
            value=current_ram, step=64,
            key=f"py_ram_{name}",
        )
    with c5:
        st.write("")
        st.write("")
        save = st.button("\U0001f4be Salvar", key=f"py_sv_{name}", type="primary", use_container_width=True)
    with c6:
        st.write("")
        st.write("")
        remove = st.button("\U0001f5d1️", key=f"py_rm_{name}", help="Remover app", use_container_width=True)

    gui_chk = st.checkbox(
        "\U0001f4fa Interface gráfica (GUI) — CREATE_BREAKAWAY_FROM_JOB",
        value=data.get("gui", False),
        key=f"py_gui_{name}",
    )

    if save:
        updated = {**data, "slot": slot, "max_ram_mb": int(ram)}
        updated["schedule"] = "loop" if enabled_chk and slot != "always" else "manual"
        if enabled_chk and slot != "always":
            updated["pause_between"] = pause_seconds
        else:
            updated.pop("pause_between", None)
        if enabled_chk and slot == "always":
            updated["auto_start"] = True
        else:
            updated.pop("auto_start", None)
        if gui_chk:
            updated["gui"] = True
        else:
            updated.pop("gui", None)
        upsert_app(CONFIG_PATH, name, updated)
        st.success(f"✅ '{name}' atualizado — hot reload em até 5s")
        st.rerun()

    if remove:
        delete_app(CONFIG_PATH, name)
        st.success(f"✅ '{name}' removido")
        st.rerun()

    # Atalhos Start/Stop
    if app_state:
        bc1, bc2, _ = st.columns([1, 1, 6])
        with bc1:
            if not app_state.enabled:
                if st.button("▶ Start", key=f"py_start_{name}"):
                    send_command("start", name)
                    st.rerun()
        with bc2:
            if app_state.enabled:
                if st.button("■ Stop", key=f"py_stop_{name}"):
                    send_command("stop", name)
                    st.rerun()

    st.divider()






# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def render_live_view(state: ControlPlaneState) -> None:
    """Aba ao vivo — stream do log da última execução do app selecionado."""
    st.markdown("### 📺 Execução ao vivo")

    apps_with_logs = list_apps_with_logs()
    # Priorizar apps em execução no topo do seletor
    running_apps = [name for name, a in state.apps.items() if a.status == "running"]
    other_apps = [a for a in apps_with_logs if a not in running_apps]
    options = running_apps + other_apps

    if not options:
        st.info("Nenhuma execução registrada ainda. Inicie um app para ver logs aqui.")
        return

    format_option = lambda name: (
        f"🟢 {name} (rodando)" if name in running_apps else f"⚪ {name}"
    )

    selected = st.selectbox(
        "App para monitorar",
        options=options,
        format_func=format_option,
        key="live_app_select",
    )

    if not selected:
        return

    log_path = get_latest_log_path(selected)
    if not log_path:
        st.info(f"Sem logs para '{selected}' ainda.")
        return

    # Metadados da última execução
    recs = read_history(selected, limit=1)
    if recs:
        r = recs[0]
        status_color = STATUS_COLORS.get(r.status, TEXT_MUTED)
        icon = STATUS_ICONS.get(r.status, "?")
        cols = st.columns(4)
        with cols[0]:
            st.markdown(
                f"**Status:** <span style='color:{status_color}'>{icon} {r.status}</span>",
                unsafe_allow_html=True,
            )
        with cols[1]:
            st.markdown(f"**Início:** {r.started_at_str}")
        with cols[2]:
            st.markdown(f"**Duração:** {format_duration(r.duration_s)}")
        with cols[3]:
            st.markdown(
                f"**Pico Memória:** {r.peak_ram_mb:.0f} MB"
                if r.peak_ram_mb > 0
                else "**Pico Memória:** —"
            )

    st.caption(f"📄 `{log_path}`")

    # Stream do log (últimos 64 KB)
    content = read_log_content(log_path, tail_kb=64)
    st.code(content, language=None, line_numbers=False)


def render_history_rich(_state: ControlPlaneState) -> None:
    """Histórico detalhado por app com drill-down em cada execução."""
    st.markdown("### \U0001f4dc Histórico detalhado")

    apps_with_logs = list_apps_with_logs()
    if not apps_with_logs:
        st.info("Nenhuma execução registrada ainda.")
        return

    # Resumo rápido: última execução de cada app
    st.markdown("#### Última execução por app")
    for app_name in apps_with_logs:
        recs = read_history(app_name, limit=1)
        if not recs:
            continue
        r = recs[0]
        color = STATUS_COLORS.get(r.status, TEXT_MUTED)
        icon = STATUS_ICONS.get(r.status, "?")
        st.markdown(
            f"<span style='color:{color}'>{icon}</span> "
            f"**{app_name}** — {r.status} — {format_duration(r.duration_s)} — "
            f"{r.started_at_str}"
            + (f" — `{r.error[:80]}`" if r.error else ""),
            unsafe_allow_html=True,
        )

    st.divider()
    st.markdown("#### Drill-down")
    selected = st.selectbox(
        "Ver histórico completo de:",
        options=apps_with_logs,
        key="history_app_select",
    )
    if not selected:
        return

    recs = read_history(selected, limit=50)
    if not recs:
        st.info(f"Sem histórico para '{selected}'.")
        return

    st.caption(f"Últimas {len(recs)} execuções de **{selected}**")
    for r in recs:
        color = STATUS_COLORS.get(r.status, TEXT_MUTED)
        icon = STATUS_ICONS.get(r.status, "?")
        label = (
            f"{icon} {r.started_at_str} — {r.status} — "
            f"{format_duration(r.duration_s)}"
            + (f" (exit {r.exit_code})" if r.exit_code not in (None, 0) else "")
        )
        with st.expander(label):
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                st.markdown(f"**exec_id:** `{r.exec_id}`")
            with c2:
                st.markdown(f"**PID:** {r.pid or '—'}")
            with c3:
                st.markdown(f"**Pico Memória:** {r.peak_ram_mb:.0f} MB")
            with c4:
                st.markdown(f"**Exit:** {r.exit_code}")
            if r.error:
                st.error(r.error)
            if r.log_file and Path(r.log_file).exists():
                st.markdown("**Log completo:**")
                st.code(read_log_content(r.log_file, tail_kb=128))
            else:
                st.caption("(arquivo de log não disponível)")


def main() -> None:
    st.set_page_config(
        page_title="Hidra Control Plane",
        page_icon="\U0001f40d",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    # Desabilitar tradutor do navegador de forma agressiva
    # (Chrome estava traduzindo RAM→bater, memória→membrana, slot→tensão)
    st.markdown(
        """
        <meta name="google" content="notranslate">
        <meta http-equiv="Content-Language" content="pt-BR">
        <script>
        // Marca body + html com notranslate logo ao carregar
        document.documentElement.setAttribute('translate', 'no');
        document.documentElement.classList.add('notranslate');
        if (document.body) {
            document.body.setAttribute('translate', 'no');
            document.body.classList.add('notranslate');
        }
        </script>
        """,
        unsafe_allow_html=True,
    )
    # CSS mínimo — o tema light já vem do .streamlit/config.toml
    st.markdown(f"""
    <style>
        html, body {{ translate: no !important; }}
        .notranslate {{ translate: no !important; }}
        div[data-testid="column"] button {{
            padding: 0.2rem 0.5rem;
            min-height: 0;
        }}
        .stProgress > div > div {{ background-color: {INFO}; }}
    </style>
    """, unsafe_allow_html=True)

    st.title("\U0001f40d Hidra Control Plane")

    tabs = st.tabs([
        "\U0001f4ca Status",
        "\U0001f4fa Ao vivo",
        "\U0001f4cb Fila",
        "⚙️ Configurar",
        "\U0001f4dc Histórico",
    ])

    with tabs[0]:
        _status_fragment()

    with tabs[1]:
        _live_fragment()

    with tabs[2]:
        _queue_fragment()

    with tabs[3]:
        # Configurar é estático — sem auto-refresh (o usuário clica em Salvar)
        render_config_tab(load_state())

    with tabs[4]:
        # Histórico é estático — o usuário usa dropdowns para drill-down
        render_history_rich(load_state())


@st.fragment(run_every=3)
def _status_fragment() -> None:
    """Fragment isolado para a aba Status — re-renderiza a cada 3s sem
    afetar o resto da página (evita NotFoundError no DOM)."""
    state = load_state()
    if not state.apps:
        st.warning("Orquestrador não iniciado ou sem apps configurados.")
        return
    render_kpi_row(state)
    st.divider()
    render_global_controls(state)
    st.divider()
    render_slots(state)
    st.divider()
    render_app_table(state)


@st.fragment(run_every=3)
def _queue_fragment() -> None:
    """Fragment isolado para a aba Fila — re-renderiza a cada 3s."""
    state = load_state()
    if not state.apps:
        st.warning("Orquestrador não iniciado.")
        return
    render_queue_view(state)


@st.fragment(run_every=5)
def _live_fragment() -> None:
    """Fragment isolado para a aba Ao vivo — re-renderiza a cada 5s
    (o log é mais caro de ler, então frequência menor)."""
    render_live_view(load_state())


if __name__ == "__main__":
    main()
