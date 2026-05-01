"""Notificações de execução — wrapper async sobre `telemonit`.

Mantém a API de `alert_failure / alert_timeout / alert_ram / alert_started /
alert_crash_restart` que o `ProcessManager` e o `Orchestrator` já consomem,
mas delega o envio (Telegram + JSONL no Drive + run tracking) para a lib
externa `telemonit`.

Por que `telemonit` em vez de chamar Telegram direto:
- Audit trail automático em JSONL no Drive (1 arquivo por projeto+mês).
- `run_id` first-class — cada execução de subprocess fica correlacionável.
- Throttle de alertas (storm protection).
- Resolução `drive:<file_id>` para credenciais — zero secrets em git.
- Ecossistema único (mesma lib usada pelos outros projetos do Marcelo).

`telemonit.{erro,alerta,info}` são funções **síncronas** (httpx sync). Para
não bloquear o event loop, todas as chamadas vão por `asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio

import telemonit
from loguru import logger

PROJETO_DEFAULT = "control_panel"


class TelegramAlerter:
    """Wrapper async sobre `telemonit` — preserva a API histórica."""

    def __init__(self, bot_token: str = "", chat_id: str = "") -> None:
        # Configuração programática: se o config.yaml trouxer token/chat_id,
        # passamos para `telemonit.configurar`. Quando vazios, a lib cai
        # automaticamente nas env vars MONITOR_TG_TOKEN / MONITOR_TG_CHAT_ID.
        kwargs: dict = {"projeto": PROJETO_DEFAULT}
        if bot_token:
            kwargs["telegram_token"] = bot_token
        if chat_id:
            kwargs["telegram_chat_id"] = chat_id
        telemonit.configurar(**kwargs)

        # `enabled` é puramente informativo agora — `telemonit` decide
        # internamente se tem credenciais para enviar Telegram.
        self.enabled = True

    async def _despachar(self, fn, **kwargs) -> None:
        """Executa uma função sync de `telemonit` em thread separada."""
        try:
            await asyncio.to_thread(fn, **kwargs)
        except Exception as exc:
            logger.error(f"Falha ao despachar via telemonit: {exc}")

    async def alert_failure(
        self,
        app_name: str,
        exit_code: int,
        error: str,
        run_id: str | None = None,
    ) -> None:
        await self._despachar(
            telemonit.erro,
            titulo=f"FALHA — {app_name}",
            detalhes=(
                f"Exit code: {exit_code}\n"
                f"Erro:\n{(error or '').strip()[:1500]}"
            ),
            contexto={
                "app": app_name,
                "exit_code": exit_code,
                "tipo": "failure",
            },
            run_id=run_id,
        )

    async def alert_timeout(
        self,
        app_name: str,
        timeout_s: int,
        run_id: str | None = None,
    ) -> None:
        await self._despachar(
            telemonit.erro,
            titulo=f"TIMEOUT — {app_name}",
            detalhes=(
                f"Excedeu o limite de {timeout_s}s. Processo encerrado pelo "
                "orquestrador."
            ),
            contexto={"app": app_name, "timeout_s": timeout_s, "tipo": "timeout"},
            run_id=run_id,
        )

    async def alert_ram(
        self,
        app_name: str,
        ram_mb: float,
        limit_mb: int,
        run_id: str | None = None,
    ) -> None:
        await self._despachar(
            telemonit.erro,
            titulo=f"RAM EXCEDIDA — {app_name}",
            detalhes=(
                f"Uso: {ram_mb:.0f} MB | Limite: {limit_mb} MB. "
                "Processo encerrado pelo orquestrador."
            ),
            contexto={
                "app": app_name,
                "ram_mb": int(ram_mb),
                "limit_mb": limit_mb,
                "tipo": "ram_exceeded",
            },
            run_id=run_id,
        )

    async def alert_started(self) -> None:
        await self._despachar(
            telemonit.info,
            titulo="Hidra Control Plane iniciado",
            detalhes="Orquestrador online — apps prontos para execução.",
            contexto={"tipo": "started"},
        )

    async def alert_crash_restart(
        self,
        app_name: str,
        run_id: str | None = None,
    ) -> None:
        await self._despachar(
            telemonit.alerta,
            titulo=f"RESTART — {app_name}",
            detalhes="Serviço crashou e foi reiniciado automaticamente.",
            contexto={"app": app_name, "tipo": "restart"},
            run_id=run_id,
        )

    # Wrapper compatível para callers eventuais que chamam send() direto.
    # Mapeia tudo para `telemonit.info` (apenas registro, sem Telegram a
    # menos que o nível mínimo esteja em `info`).
    async def send(self, message: str) -> None:
        await self._despachar(
            telemonit.info,
            titulo="Mensagem do orquestrador",
            detalhes=message,
            contexto={"tipo": "send_legado"},
        )
