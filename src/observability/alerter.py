"""Notificações Telegram para falhas, timeouts e RAM excedida."""

from __future__ import annotations

import asyncio
from datetime import datetime

import httpx
from loguru import logger


class TelegramAlerter:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)
        if not self.enabled:
            logger.warning("Telegram alerter desabilitado — token ou chat_id vazio")

    async def send(self, message: str) -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code != 200:
                    logger.error(f"Telegram API erro {resp.status_code}: {resp.text}")
        except Exception as e:
            logger.error(f"Falha ao enviar alerta Telegram: {e}")

    async def alert_failure(self, app_name: str, exit_code: int, error: str) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        msg = (
            f"<b>FALHA</b> {app_name}\n"
            f"Exit code: {exit_code}\n"
            f"Hora: {now}\n"
            f"Erro: <pre>{error[:500]}</pre>"
        )
        await self.send(msg)

    async def alert_timeout(self, app_name: str, timeout_s: int) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        msg = (
            f"<b>TIMEOUT</b> {app_name}\n"
            f"Limite: {timeout_s}s\n"
            f"Hora: {now}\n"
            f"Processo foi encerrado (kill)."
        )
        await self.send(msg)

    async def alert_ram(self, app_name: str, ram_mb: float, limit_mb: int) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        msg = (
            f"<b>RAM EXCEDIDA</b> {app_name}\n"
            f"Uso: {ram_mb:.0f} MB / Limite: {limit_mb} MB\n"
            f"Hora: {now}\n"
            f"Processo foi encerrado (kill)."
        )
        await self.send(msg)

    async def alert_started(self) -> None:
        now = datetime.now().strftime("%d/%m %H:%M")
        await self.send(f"Hidra Control Plane iniciado em {now}")

    async def alert_crash_restart(self, app_name: str) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        msg = (
            f"<b>RESTART</b> {app_name}\n"
            f"Hora: {now}\n"
            f"Servi\u00e7o crashou e foi reiniciado automaticamente."
        )
        await self.send(msg)
