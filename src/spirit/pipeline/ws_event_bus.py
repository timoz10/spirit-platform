"""
WsEventBus: WebSocket client implementation of PipelineEventBus.

Api-driven Spirit runtimes connect to the gateway's /v1/events endpoint
instead of opening a direct PG LISTEN. The gateway is the only PG consumer;
it fans events out over a multiplexed WebSocket. See
`docs/features/platform/GATEWAY_WS_ARCHITECTURE.md` for the full design.

Threading model mirrors PgEventBus so the two are interchangeable behind the
PipelineEventBus protocol:

  - Public API (subscribe / wait_for / start / stop) is sync + thread-safe.
  - Internal asyncio loop runs in a dedicated daemon thread.
  - Cross-thread coordination uses locks on the mutable state (subscribers,
    waiters, last_event_id) and `run_coroutine_threadsafe` for sends.

Resume + reconnect:
  - Last event_id per channel is tracked and replayed on reconnect as
    ``{"name": "<ch>", "since": N}`` in the subscribe op.
  - Exponential backoff with ±25% jitter between sessions (1s → 60s).
  - On ``{"type": "gap"}`` the event is logged; downstream consumers that
    care about strict ordering must refresh state via REST (escape hatch).
"""

from __future__ import annotations

import asyncio
import json
import random
import threading
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

import websockets
from websockets.exceptions import ConnectionClosed

from spirit.logger import get_logger
from spirit.pipeline.event_bus import EventCallback, PipelineEvent

logger = get_logger("ws_event_bus")


# Client-side mirror of the gateway's PG → WS channel namespace map.
# Keep in sync with spirit.api.ws.protocol.PG_TO_WS_CHANNEL. Duplicated here
# rather than imported so Spirit clients don't pull in gateway code.
PG_TO_WS_CHANNEL: Dict[str, str] = {
    "pipeline_ohlc": "fw.pipeline_ohlc",
    "pair_registry": "fw.pair_registry",
    "heartbeats": "fw.heartbeats",
    "pipeline_dlimit_60m": "ip.pipeline_dlimit_60m",
    "pipeline_dlimit_15m": "ip.pipeline_dlimit_15m",
    "pipeline_bounce_physics": "ip.pipeline_bounce_physics",
}

# PipelineEvent dataclass fields — used to filter payload kwargs defensively
# so an unexpected key (e.g. added upstream) doesn't TypeError the client.
_PIPELINE_EVENT_FIELDS = frozenset({
    "stage",
    "pair",
    "interval_minutes",
    "candle_dt",
    "rows_affected",
    "duration_ms",
    "metadata",
})


def derive_ws_url(api_url: str) -> str:
    """Convert an http(s) API base URL into the ws(s) /events URL.

    The gateway exposes ``/v1/events``; SPIRIT_API_URL already points at the
    ``/v1`` base (e.g. ``http://10.0.0.4:8000/v1``), so we just swap scheme
    and append ``/events``.
    """
    parsed = urlparse(api_url.rstrip("/"))
    scheme_map = {"http": "ws", "https": "wss"}
    scheme = scheme_map.get(parsed.scheme, parsed.scheme)
    path = parsed.path.rstrip("/") + "/events"
    return urlunparse((scheme, parsed.netloc, path, "", "", ""))


class WsEventBus:
    """Client-side pipeline event bus over WebSocket to the gateway.

    Implements the PipelineEventBus protocol. ``publish()`` is not supported —
    only the gateway writes to PG; Spirit clients consume.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        reconnect_min: float = 1.0,
        reconnect_max: float = 60.0,
        open_timeout: float = 10.0,
    ) -> None:
        if not url:
            raise ValueError("WsEventBus: url is required")
        if not api_key:
            raise ValueError("WsEventBus: api_key is required")

        self._url = url
        self._api_key = api_key
        self._reconnect_min = reconnect_min
        self._reconnect_max = reconnect_max
        self._open_timeout = open_timeout

        self._subscribers: Dict[str, List[EventCallback]] = defaultdict(list)
        self._raw_subscribers: Dict[str, List[Callable[[str, str], None]]] = defaultdict(list)
        self._waiters: Dict[str, list] = defaultdict(list)
        self._last_event_id: Dict[str, int] = {}
        self._lock = threading.Lock()

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._current_ws = None  # type: Any
        self._ready_event = threading.Event()
        self._started = False

    # ------------------------------------------------------------------
    # PipelineEventBus protocol
    # ------------------------------------------------------------------

    def publish(self, event: PipelineEvent) -> None:
        raise NotImplementedError(
            "WsEventBus.publish() is not supported — the gateway is the only "
            "PG publisher. Spirit clients consume events only."
        )

    def subscribe(self, channel: str, callback: EventCallback) -> None:
        """Register a callback for PipelineEvent arrivals on ``channel``.

        ``channel`` is the PG NOTIFY channel name (e.g. ``pipeline_dlimit_60m``);
        the bus maps it to the gateway's namespaced WS channel internally.
        """
        with self._lock:
            self._subscribers[channel].append(callback)
        self._send_subscribe_if_live([channel])

    def subscribe_raw(
        self, channel: str, callback: Callable[[str, str], None]
    ) -> None:
        """Register a callback that receives ``(channel, raw_payload_json)``.

        Matches PgEventBus.subscribe_raw — used for non-PipelineEvent channels
        like ``pair_registry`` where the payload isn't a PipelineEvent.
        """
        with self._lock:
            self._raw_subscribers[channel].append(callback)
        self._send_subscribe_if_live([channel])

    def wait_for(
        self,
        channel: str,
        pair: str,
        candle_dt: str,
        timeout: float = 60.0,
    ) -> Optional[PipelineEvent]:
        """Block until a matching event arrives or timeout. Thread-safe."""
        waiter_event = threading.Event()
        waiter = {
            "pair": pair,
            "candle_dt": candle_dt,
            "event": None,
            "waiter": waiter_event,
        }

        with self._lock:
            self._waiters[channel].append(waiter)
            needs_subscribe = channel not in self._subscribers and channel not in self._raw_subscribers

        if needs_subscribe:
            self._send_subscribe_if_live([channel])

        waiter_event.wait(timeout=timeout)

        with self._lock:
            try:
                self._waiters[channel].remove(waiter)
            except ValueError:
                pass

        return waiter["event"]

    def start(self) -> None:
        if self._started:
            logger.warning("[START] WsEventBus already running")
            return
        self._started = True
        self._ready_event.clear()

        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop,
            name="WsEventBus-Loop",
            daemon=True,
        )
        self._loop_thread.start()
        # Give the loop a moment to install itself — so subsequent calls to
        # _send_subscribe_if_live can safely schedule on it.
        self._ready_event.wait(timeout=5)
        logger.info("[START] WsEventBus loop started (url=%s)", self._url)

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False

        loop = self._loop
        stop_event = self._stop_event
        if loop is not None and stop_event is not None:
            try:
                loop.call_soon_threadsafe(stop_event.set)
            except RuntimeError:
                pass

        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5)
            self._loop_thread = None

        if loop is not None:
            try:
                loop.close()
            except Exception:
                pass
        self._loop = None
        self._stop_event = None
        self._current_ws = None
        logger.info("[STOP] WsEventBus stopped")

    # ------------------------------------------------------------------
    # Async loop
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()
        self._ready_event.set()
        try:
            self._loop.run_until_complete(self._main())
        except Exception as e:  # pragma: no cover — safety net
            logger.error("[LOOP] Terminated unexpectedly: %s", e)

    async def _main(self) -> None:
        assert self._stop_event is not None
        delay = self._reconnect_min
        while not self._stop_event.is_set():
            try:
                await self._session()
                delay = self._reconnect_min
            except asyncio.CancelledError:
                raise
            except Exception as e:
                jittered = delay * (1 + random.uniform(-0.25, 0.25))
                logger.warning(
                    "[WS] Session ended (%s) — reconnect in %.1fs",
                    e, jittered,
                )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=jittered
                    )
                    return  # stop requested during backoff
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, self._reconnect_max)

    async def _session(self) -> None:
        """Open one WS session and serve until close or stop."""
        subprotocols = ["spirit.v1", f"apikey.{self._api_key}"]
        async with websockets.connect(
            self._url,
            subprotocols=subprotocols,
            open_timeout=self._open_timeout,
            ping_interval=None,  # gateway drives app-level heartbeat
            close_timeout=5,
        ) as ws:
            self._current_ws = ws
            logger.info("[WS] Connected to %s", self._url)

            try:
                await self._resubscribe_all(ws)

                assert self._stop_event is not None
                stop_task = asyncio.create_task(self._stop_event.wait())
                reader_task = asyncio.create_task(self._reader_loop(ws))

                done, pending = await asyncio.wait(
                    {stop_task, reader_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                for t in done:
                    exc = t.exception()
                    if exc is not None and not isinstance(exc, asyncio.CancelledError):
                        raise exc
            finally:
                self._current_ws = None

    async def _resubscribe_all(self, ws) -> None:
        """Send a single subscribe op covering every registered channel."""
        with self._lock:
            pg_channels = sorted({*self._subscribers.keys(), *self._raw_subscribers.keys()})
            last = dict(self._last_event_id)

        entries = []
        for pg in pg_channels:
            ws_ch = PG_TO_WS_CHANNEL.get(pg)
            if ws_ch is None:
                logger.warning("[WS] Unknown PG channel %s — skipping subscribe", pg)
                continue
            entry: Dict[str, Any] = {"name": ws_ch}
            since = last.get(pg)
            if since:
                entry["since"] = since
            entries.append(entry)

        if entries:
            await ws.send(json.dumps({"op": "subscribe", "channels": entries}))
            logger.debug("[WS] Sent subscribe for %d channels", len(entries))

    async def _reader_loop(self, ws) -> None:
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("[WS] Non-JSON message: %r", str(raw)[:200])
                    continue
                self._handle_message(msg)
        except ConnectionClosed:
            # Normal close — bubble up so _main can reconnect.
            raise

    # ------------------------------------------------------------------
    # Message dispatch
    # ------------------------------------------------------------------

    def _handle_message(self, msg: dict) -> None:
        msg_type = msg.get("type")
        if msg_type == "event":
            self._dispatch_event(msg)
        elif msg_type == "ready":
            logger.debug("[WS] ready channel=%s", msg.get("channel"))
        elif msg_type == "error":
            logger.warning(
                "[WS] error channel=%s msg=%s",
                msg.get("channel"), msg.get("msg"),
            )
        elif msg_type == "gap":
            logger.warning(
                "[WS] GAP channel-level: after=%s current=%s — "
                "downstream should refresh state via REST",
                msg.get("after"), msg.get("current"),
            )
        elif msg_type == "ping":
            # Gateway heartbeat — noop ack (server doesn't require a reply;
            # connection liveness is checked on the send side).
            pass
        elif msg_type == "pong":
            pass
        else:
            logger.debug("[WS] unknown type=%s", msg_type)

    def _dispatch_event(self, msg: dict) -> None:
        ws_channel = msg.get("channel") or ""
        pg_channel = ws_channel.split(".", 1)[-1] if "." in ws_channel else ws_channel
        try:
            event_id = int(msg.get("event_id", 0))
        except (TypeError, ValueError):
            event_id = 0
        payload = msg.get("payload") or {}

        # Update resume cursor for this channel
        with self._lock:
            current = self._last_event_id.get(pg_channel, 0)
            if event_id > current:
                self._last_event_id[pg_channel] = event_id

        # Raw subscribers: PgEventBus passes through the original NOTIFY text.
        # Over WS we've lost the raw bytes, but the gateway bridge preserves
        # non-JSON PG payloads as {"raw": <original>} — unwrap that case so
        # raw callbacks see what they would have via PG LISTEN.
        with self._lock:
            raw_cbs = list(self._raw_subscribers.get(pg_channel, []))

        if raw_cbs:
            if isinstance(payload, dict) and set(payload.keys()) == {"raw"}:
                raw_str = str(payload["raw"])
            else:
                raw_str = json.dumps(payload)
            for cb in raw_cbs:
                try:
                    cb(pg_channel, raw_str)
                except Exception as e:
                    logger.error(
                        "[DISPATCH_RAW] Callback error on %s: %s", pg_channel, e
                    )

        # Structured PipelineEvent reconstruction.
        event = self._parse_pipeline_event(payload)
        if event is None:
            if not raw_cbs:
                logger.debug(
                    "[DISPATCH] Non-PipelineEvent payload on %s — no subscribers",
                    pg_channel,
                )
            return

        with self._lock:
            callbacks = list(self._subscribers.get(pg_channel, []))
        for cb in callbacks:
            try:
                cb(event)
            except Exception as e:
                logger.error("[DISPATCH] Callback error on %s: %s", pg_channel, e)

        # Wake matching waiters
        with self._lock:
            remaining = []
            for waiter in self._waiters.get(pg_channel, []):
                if (
                    waiter["pair"] == event.pair
                    and waiter["candle_dt"] == event.candle_dt
                ):
                    waiter["event"] = event
                    waiter["waiter"].set()
                else:
                    remaining.append(waiter)
            self._waiters[pg_channel] = remaining

    @staticmethod
    def _parse_pipeline_event(payload: Any) -> Optional[PipelineEvent]:
        if not isinstance(payload, dict):
            return None
        kwargs = {k: v for k, v in payload.items() if k in _PIPELINE_EVENT_FIELDS}
        if "stage" not in kwargs or "pair" not in kwargs or "candle_dt" not in kwargs:
            return None
        kwargs.setdefault("interval_minutes", 0)
        try:
            return PipelineEvent(**kwargs)
        except TypeError:
            return None

    # ------------------------------------------------------------------
    # Incremental subscribe
    # ------------------------------------------------------------------

    def _send_subscribe_if_live(self, channels: List[str]) -> None:
        """If a session is open, send a subscribe op for the new channels.

        If the loop isn't running yet or no WS session is open, the channel is
        already registered in _subscribers / _raw_subscribers and will be picked
        up by _resubscribe_all on the next session.
        """
        if self._loop is None or not self._ready_event.is_set():
            return
        if self._current_ws is None:
            return

        with self._lock:
            last = dict(self._last_event_id)

        entries: List[Dict[str, Any]] = []
        for pg in channels:
            ws_ch = PG_TO_WS_CHANNEL.get(pg)
            if ws_ch is None:
                continue
            entry: Dict[str, Any] = {"name": ws_ch}
            since = last.get(pg)
            if since:
                entry["since"] = since
            entries.append(entry)

        if not entries:
            return

        async def _send():
            ws = self._current_ws
            if ws is None:
                return
            try:
                await ws.send(json.dumps({"op": "subscribe", "channels": entries}))
            except Exception as e:
                logger.debug("[WS] Incremental subscribe failed: %s", e)

        try:
            asyncio.run_coroutine_threadsafe(_send(), self._loop)
        except RuntimeError as e:
            logger.debug("[WS] Subscribe schedule failed: %s", e)
