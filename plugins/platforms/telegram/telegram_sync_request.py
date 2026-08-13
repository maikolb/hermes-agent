"""Stdlib-backed Telegram Bot API request transport.

The host can intermittently reach api.telegram.org through urllib while the
httpx async/sync stacks time out. This adapter preserves python-telegram-bot's
BaseRequest contract and executes urllib in worker threads. Automatic retries
are restricted to idempotent Bot API methods, so ambiguous send timeouts never
duplicate user-visible messages.
"""

from __future__ import annotations

import asyncio
import time
import urllib.error
import urllib.parse
import urllib.request

from telegram.error import NetworkError, TimedOut
from telegram.request import BaseRequest, RequestData


_IDEMPOTENT_METHODS = {
    "getme",
    "getupdates",
    "deletewebhook",
    "getwebhookinfo",
    "setmycommands",
    "getmycommands",
}


class ThreadedUrllibRequest(BaseRequest):
    """PTB request contract implemented with stdlib urllib worker threads."""

    def __init__(
        self,
        *,
        connection_pool_size: int = 32,
        read_timeout: float | None = 20.0,
        write_timeout: float | None = 20.0,
        connect_timeout: float | None = 15.0,
        pool_timeout: float | None = 5.0,
    ) -> None:
        del connection_pool_size, write_timeout, pool_timeout
        self._read_timeout = read_timeout
        self._connect_timeout = connect_timeout
        self._initialized = False

    @property
    def read_timeout(self) -> float | None:
        return self._read_timeout

    async def initialize(self) -> None:
        self._initialized = True

    async def shutdown(self) -> None:
        self._initialized = False

    @staticmethod
    def _is_default(value) -> bool:
        return value is BaseRequest.DEFAULT_NONE or value.__class__.__name__ == "DefaultValue"

    @staticmethod
    def _api_method(url: str) -> str:
        return url.rsplit("/", 1)[-1].split("?", 1)[0].lower()

    async def do_request(
        self,
        url: str,
        method: str,
        request_data: RequestData | None = None,
        read_timeout=BaseRequest.DEFAULT_NONE,
        write_timeout=BaseRequest.DEFAULT_NONE,
        connect_timeout=BaseRequest.DEFAULT_NONE,
        pool_timeout=BaseRequest.DEFAULT_NONE,
    ) -> tuple[int, bytes]:
        del write_timeout, pool_timeout
        if not self._initialized:
            raise RuntimeError("ThreadedUrllibRequest is not initialized")
        if request_data and request_data.contains_files:
            raise NetworkError("Stdlib Telegram transport does not support file uploads")

        resolved_read = self._read_timeout if self._is_default(read_timeout) else read_timeout
        resolved_connect = self._connect_timeout if self._is_default(connect_timeout) else connect_timeout
        candidates = [v for v in (resolved_read, resolved_connect) if isinstance(v, (int, float))]
        timeout = max(candidates) if candidates else None
        # Long polling passes read_timeout above the Bot API timeout; preserve it.
        if timeout is not None:
            timeout = max(float(timeout), 1.0)

        data = None
        if request_data:
            data = urllib.parse.urlencode(request_data.json_parameters).encode("utf-8")
        api_method = self._api_method(url)
        attempts = 3 if api_method in _IDEMPOTENT_METHODS else 1

        def _send() -> tuple[int, bytes]:
            request = urllib.request.Request(
                url,
                data=data,
                method=method,
                headers={
                    "User-Agent": self.USER_AGENT,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return int(response.status), response.read()

        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                return await asyncio.to_thread(_send)
            except urllib.error.HTTPError as exc:
                return int(exc.code), exc.read()
            except (TimeoutError, urllib.error.URLError, OSError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(min(1.0 * (attempt + 1), 2.0))

        if isinstance(last_error, (TimeoutError, urllib.error.URLError)) and "timed out" in str(last_error).lower():
            raise TimedOut from last_error
        raise NetworkError(f"urllib.{last_error.__class__.__name__}: {last_error}") from last_error
