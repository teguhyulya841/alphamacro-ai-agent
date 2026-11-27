"""Deterministic threshold alert evaluator & async digest scheduler."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic import AwareDatetime, ValidationError

from alphamacro.audit import AuditLog
from alphamacro.clock import SystemClock
from alphamacro.delivery.telegram import (
    TelegramDeliveryClient,
    format_alert_message,
    format_digest_message,
)
from alphamacro.infra.state import StateBundle
from alphamacro.models import (
    AgentResult,
    AlertEvent,
    AlertOperator,
    AlertRule,
    CacheClass,
    CacheEntry,
    CrossValidationReport,
    DeliveryReceipt,
    DigestSchedule,
    JsonValue,
)
from alphamacro.ports import Clock
from alphamacro.tools.coingecko import AsyncCoinGeckoClient
from alphamacro.tools.technical import async_fetch_snapshot
from alphamacro.tools.venues import MultiVenueValidator

logger = logging.getLogger(__name__)

_RULES_CACHE_KEY = "scheduler:rules"
_RULES_RETAIN_S = 86400 * 365
_DIGEST_RETAIN_S = 86400 * 30


def evaluate_alert_rule(
    rule: AlertRule,
    *,
    snapshot: dict[str, Any] | None,
    indicator_snapshot: dict[str, Any] | None = None,
    cross_validation: CrossValidationReport | None = None,
    now: AwareDatetime,
) -> AlertEvent | None:
    """Evaluate a single AlertRule purely against observed numeric metrics."""
    if not rule.enabled:
        return None

    observed: float | None = None
    triggered = False
    summary = ""

    op = rule.operator
    if op in (AlertOperator.PRICE_GT, AlertOperator.PRICE_LT) and snapshot:
        raw = snapshot.get("current_price")
        if raw is not None:
            observed = float(raw)
            triggered = (
                observed > rule.threshold
                if op is AlertOperator.PRICE_GT
                else observed < rule.threshold
            )
            direction = (
                "menembus di atas" if op is AlertOperator.PRICE_GT else "turun di bawah"
            )
            summary = (
                f"Harga {rule.coin_id} (${observed:,.2f}) {direction} "
                f"ambang ${rule.threshold:,.2f}."
            )
    elif op in (AlertOperator.CHANGE_24H_GT, AlertOperator.CHANGE_24H_LT) and snapshot:
        raw = snapshot.get("price_change_percentage_24h")
        if raw is not None:
            observed = float(raw)
            triggered = (
                observed > rule.threshold
                if op is AlertOperator.CHANGE_24H_GT
                else observed < rule.threshold
            )
            direction = (
                "naik melebihi"
                if op is AlertOperator.CHANGE_24H_GT
                else "turun di bawah"
            )
            summary = (
                f"Perubahan 24H {rule.coin_id} ({observed:+.2f}%) {direction} "
                f"{rule.threshold:+.2f}%."
            )
    elif op in (AlertOperator.RSI_GT, AlertOperator.RSI_LT) and indicator_snapshot:
        raw = indicator_snapshot.get("rsi14")
        if raw is not None:
            observed = float(raw)
            triggered = (
                observed > rule.threshold
                if op is AlertOperator.RSI_GT
                else observed < rule.threshold
            )
            direction = "di atas" if op is AlertOperator.RSI_GT else "di bawah"
            summary = (
                f"RSI(14) daily {rule.coin_id} ({observed:.2f}) berada {direction} "
                f"{rule.threshold:.2f}."
            )
    elif op is AlertOperator.SPREAD_GT and cross_validation is not None:
        observed = float(cross_validation.max_spread_pct)
        triggered = observed > rule.threshold
        summary = (
            f"Spread antar-venue {rule.coin_id} mencapai {observed:.2f}% "
            f"(batas {rule.threshold:.2f}%)."
        )

    if not triggered or observed is None:
        return None

    return AlertEvent(
        rule_id=rule.rule_id,
        coin_id=rule.coin_id,
        operator=rule.operator,
        threshold=rule.threshold,
        observed_value=round(observed, 6),
        triggered_at=now,
        summary=summary,
        cross_validation=cross_validation,
    )


class ResearchScheduler:
    """Coordinates threshold alerts and periodic digests with distributed locks."""

    def __init__(
        self,
        *,
        engine: Any,
        market_client: AsyncCoinGeckoClient,
        delivery: TelegramDeliveryClient | None = None,
        default_chat_id: str | None = None,
        venue_validator: MultiVenueValidator | None = None,
        state_bundle: StateBundle | None = None,
        clock: Clock | None = None,
        audit: AuditLog | None = None,
        poll_interval_s: float = 60.0,
        max_rules: int = 200,
    ) -> None:
        self._engine = engine
        self._market = market_client
        self._delivery = delivery
        self._chat_id = default_chat_id
        self._validator = venue_validator
        self._state = state_bundle
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._audit = audit
        self._poll_interval_s = poll_interval_s
        self._max_rules = max(1, max_rules)

        self._rules: dict[str, AlertRule] = {}
        self._digests: dict[str, DigestSchedule] = {}
        self._last_fired_epoch: dict[str, float] = {}
        self._last_digest_epoch: dict[str, float] = {}
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # -- rule & digest management -----------------------------------------

    def upsert_rule(self, rule: AlertRule) -> AlertRule:
        if rule.rule_id not in self._rules and len(self._rules) >= self._max_rules:
            raise ValueError(
                f"Maximum number of alert rules ({self._max_rules}) reached."
            )
        self._rules[rule.rule_id] = rule
        return rule

    def remove_rule(self, rule_id: str) -> bool:
        return self._rules.pop(rule_id, None) is not None

    def list_rules(self) -> list[AlertRule]:
        return list(self._rules.values())

    async def _load_persisted_rules(self) -> dict[str, AlertRule]:
        if self._state is None:
            return dict(self._rules)
        entry = await self._state.cache.get(_RULES_CACHE_KEY)
        loaded: dict[str, AlertRule] = {}
        if entry is not None and isinstance(entry.value, dict):
            for rule_id, raw_rule in entry.value.items():
                try:
                    loaded[str(rule_id)] = AlertRule.model_validate(raw_rule)
                except (ValidationError, TypeError, ValueError):
                    logger.warning("Ignoring invalid persisted alert rule %r", rule_id)
        loaded.update(self._rules)
        return loaded

    async def _save_persisted_rules(self, rules: dict[str, AlertRule]) -> None:
        if self._state is None:
            return
        now = self._clock.now()
        payload: dict[str, JsonValue] = {
            rid: r.model_dump(mode="json") for rid, r in rules.items()
        }
        entry = CacheEntry(
            key=_RULES_CACHE_KEY,
            cache_class=CacheClass.DAILY_CHART,
            value=payload,
            fetched_at=now,
            source="scheduler",
        )
        await self._state.cache.put(entry, retain_s=_RULES_RETAIN_S)

    async def aupsert_rule(self, rule: AlertRule) -> AlertRule:
        rules = await self._load_persisted_rules()
        if rule.rule_id not in rules and len(rules) >= self._max_rules:
            raise ValueError(
                f"Maximum number of alert rules ({self._max_rules}) reached."
            )
        rules[rule.rule_id] = rule
        self._rules[rule.rule_id] = rule
        await self._save_persisted_rules(rules)
        return rule

    async def aremove_rule(self, rule_id: str) -> bool:
        rules = await self._load_persisted_rules()
        removed = rules.pop(rule_id, None) is not None
        self._rules.pop(rule_id, None)
        if removed:
            await self._save_persisted_rules(rules)
        return removed

    async def alist_rules(self) -> list[AlertRule]:
        rules = await self._load_persisted_rules()
        self._rules = rules
        return list(rules.values())

    def upsert_digest(self, schedule: DigestSchedule) -> DigestSchedule:
        self._digests[schedule.digest_id] = schedule
        return schedule

    def list_digests(self) -> list[DigestSchedule]:
        return list(self._digests.values())

    # -- cooldown helpers -------------------------------------------------

    async def _is_in_cooldown(self, rule: AlertRule, now: AwareDatetime) -> bool:
        now_epoch = now.timestamp()
        mem_last = self._last_fired_epoch.get(rule.rule_id)
        if mem_last is not None and (now_epoch - mem_last) < rule.cooldown_s:
            return True
        if self._state is not None:
            entry = await self._state.cache.get(f"alert:cooldown:{rule.rule_id}")
            if entry is not None and entry.age_s(now) < rule.cooldown_s:
                return True
        return False

    async def _mark_fired(self, rule: AlertRule, now: AwareDatetime) -> None:
        self._last_fired_epoch[rule.rule_id] = now.timestamp()
        if self._state is not None:
            entry = CacheEntry(
                key=f"alert:cooldown:{rule.rule_id}",
                cache_class=CacheClass.TICKER,
                value={"rule_id": rule.rule_id, "fired_at": now.isoformat()},
                fetched_at=now,
                source="scheduler",
            )
            await self._state.cache.put(entry, retain_s=rule.cooldown_s)

    async def _is_digest_due(
        self, schedule: DigestSchedule, now: AwareDatetime
    ) -> bool:
        now_epoch = now.timestamp()
        last = self._last_digest_epoch.get(schedule.digest_id)
        if last is not None and (now_epoch - last) < schedule.interval_s:
            return False
        if self._state is not None:
            entry = await self._state.cache.get(f"digest:last:{schedule.digest_id}")
            if entry is not None and entry.age_s(now) < schedule.interval_s:
                self._last_digest_epoch[schedule.digest_id] = (
                    entry.fetched_at.timestamp()
                )
                return False
        return True

    async def _mark_digest_ran(
        self, schedule: DigestSchedule, now: AwareDatetime
    ) -> None:
        self._last_digest_epoch[schedule.digest_id] = now.timestamp()
        if self._state is not None:
            entry = CacheEntry(
                key=f"digest:last:{schedule.digest_id}",
                cache_class=CacheClass.DAILY_CHART,
                value={"digest_id": schedule.digest_id, "ran_at": now.isoformat()},
                fetched_at=now,
                source="scheduler",
            )
            await self._state.cache.put(
                entry, retain_s=max(schedule.interval_s, _DIGEST_RETAIN_S)
            )

    # -- alert & digest execution -----------------------------------------

    async def run_alert_check(self, *, chat_id: str | None = None) -> list[AlertEvent]:
        """Evaluate all enabled rules once and deliver triggered alerts."""
        all_rules = await self.alist_rules()
        active_rules = [r for r in all_rules if r.enabled]
        if not active_rules:
            return []

        lease = None
        if self._state is not None:
            lease = await self._state.locks.acquire("scheduler:alerts", ttl_s=30)
            if lease is None:
                logger.debug("Another worker holds scheduler:alerts lease; skipping")
                return []

        try:
            now = self._clock.now()
            coin_ids = sorted({r.coin_id for r in active_rules})
            snapshots = await self._market.get_market_snapshots(coin_ids)

            rsi_coins = sorted(
                {
                    r.coin_id
                    for r in active_rules
                    if r.operator in (AlertOperator.RSI_GT, AlertOperator.RSI_LT)
                }
            )
            indicators_by_coin: dict[str, dict[str, Any]] = {}
            for coin_id in rsi_coins:
                try:
                    indicators_by_coin[coin_id] = await async_fetch_snapshot(
                        self._market, coin_id, "daily"
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed fetching daily indicators for %s: %s",
                        coin_id,
                        exc,
                    )

            cv_by_coin: dict[str, CrossValidationReport] = {}
            if self._validator is not None:
                for coin_id in coin_ids:
                    snap = snapshots.get(coin_id)
                    ref_price = (
                        float(snap["current_price"])
                        if snap and snap.get("current_price")
                        else None
                    )
                    if ref_price is not None and ref_price > 0:
                        cv_by_coin[coin_id] = await self._validator.cross_validate(
                            coin_id, reference_price_usd=ref_price
                        )

            triggered_events: list[AlertEvent] = []
            target_chat = chat_id or self._chat_id

            for rule in active_rules:
                if await self._is_in_cooldown(rule, now):
                    continue
                event = evaluate_alert_rule(
                    rule,
                    snapshot=snapshots.get(rule.coin_id),
                    indicator_snapshot=indicators_by_coin.get(rule.coin_id),
                    cross_validation=cv_by_coin.get(rule.coin_id),
                    now=now,
                )
                if event is None:
                    continue

                await self._mark_fired(rule, now)
                triggered_events.append(event)

                receipt: DeliveryReceipt | None = None
                if self._delivery is not None and target_chat:
                    msg = format_alert_message(event)
                    receipt = await self._delivery.send_message(target_chat, msg)

                if self._audit is not None:
                    await asyncio.to_thread(
                        self._audit.record,
                        {
                            "event_type": "alert_triggered",
                            "rule_id": event.rule_id,
                            "coin_id": event.coin_id,
                            "operator": event.operator.value,
                            "observed_value": event.observed_value,
                            "threshold": event.threshold,
                            "delivered": receipt.delivered if receipt else False,
                        },
                    )

            return triggered_events
        finally:
            if lease is not None and self._state is not None:
                await self._state.locks.release(lease)

    async def run_digest(
        self,
        schedule: DigestSchedule,
        *,
        chat_id: str | None = None,
    ) -> AgentResult | None:
        """Execute a single research digest and deliver it to Telegram."""
        if not schedule.enabled:
            return None

        lease = None
        if self._state is not None:
            lease = await self._state.locks.acquire(
                f"scheduler:digest:{schedule.digest_id}", ttl_s=120
            )
            if lease is None:
                logger.debug(
                    "Digest %s lock held by another worker; skipping",
                    schedule.digest_id,
                )
                return None

        try:
            now = self._clock.now()
            result: AgentResult = await self._engine.arun(
                schedule.query,
                user_id=f"digest:{schedule.digest_id}",
                channel="scheduler",
            )
            await self._mark_digest_ran(schedule, now)

            target_chat = chat_id or self._chat_id
            if self._delivery is not None and target_chat:
                await self._delivery.send_message(
                    target_chat, format_digest_message(result)
                )
            return result
        finally:
            if lease is not None and self._state is not None:
                await self._state.locks.release(lease)

    async def tick_once(self) -> None:
        """Run one scheduler cycle: evaluate alerts + due digests."""
        await self.run_alert_check()
        now = self._clock.now()
        for schedule in list(self._digests.values()):
            if not schedule.enabled:
                continue
            if await self._is_digest_due(schedule, now):
                await self.run_digest(schedule)

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.tick_once()
            except Exception:
                logger.exception("Error during ResearchScheduler tick")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._poll_interval_s
                )
            except TimeoutError:
                continue

    def start(self) -> None:
        """Start background polling loop if not already running."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._loop(), name="alphamacro-research-scheduler"
        )

    async def stop(self) -> None:
        """Signal background loop to stop and await graceful shutdown."""
        self._stop_event.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
