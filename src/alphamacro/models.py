"""Data models and value objects shared across the application."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PositiveInt,
    StringConstraints,
    model_validator,
)

from alphamacro.constants import DISCLAIMER

Category = Literal["MACRO", "CRYPTO_MARKET", "TECHNICAL", "FULL_RESEARCH"]
type CoinId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
type Unit = Annotated[float, Field(ge=0.0, le=1.0)]

_UNSET: Any = object()


class Frozen(BaseModel):
    """Immutable value object that rejects unknown fields at boundaries."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class StateBackend(StrEnum):
    MEMORY = "memory"
    SQLITE = "sqlite"
    REDIS = "redis"


class DegradedMode(StrEnum):
    FAIL_OPEN = "fail_open"
    FAIL_CLOSED = "fail_closed"


class CacheClass(StrEnum):
    MARKETS = "markets"
    DAILY_CHART = "daily_chart"
    OHLC_4H = "ohlc_4h"
    TICKER = "ticker"


class Freshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    EXPIRED = "expired"


class ConfidenceLevel(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class AlertOperator(StrEnum):
    PRICE_GT = "price_gt"
    PRICE_LT = "price_lt"
    CHANGE_24H_GT = "change_24h_gt"
    CHANGE_24H_LT = "change_24h_lt"
    RSI_GT = "rsi_gt"
    RSI_LT = "rsi_lt"
    SPREAD_GT = "spread_gt"


class FreshnessPolicy(Frozen):
    fresh_ttl_s: PositiveInt
    stale_ttl_s: PositiveInt

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.stale_ttl_s < self.fresh_ttl_s:
            raise ValueError("stale_ttl_s must be >= fresh_ttl_s")
        return self


class CacheEntry(Frozen):
    key: str
    cache_class: CacheClass
    value: JsonValue
    fetched_at: AwareDatetime
    source: str
    schema_version: int = 1

    def age_s(self, now: AwareDatetime) -> float:
        return max(0.0, (now - self.fetched_at).total_seconds())

    def freshness(self, policy: FreshnessPolicy, now: AwareDatetime) -> Freshness:
        age = self.age_s(now)
        if age <= policy.fresh_ttl_s:
            return Freshness.FRESH
        return Freshness.STALE if age <= policy.stale_ttl_s else Freshness.EXPIRED


class RateRule(Frozen):
    limit: PositiveInt
    window_s: PositiveInt


class RateDecision(Frozen):
    allowed: bool
    remaining: int = Field(ge=0)
    retry_after_s: float = Field(ge=0.0)


class Lease(Frozen):
    name: str
    owner: str
    expires_at: AwareDatetime


class VenueQuote(Frozen):
    """Spot price quote fetched from a secondary exchange venue."""

    venue: str
    symbol: str
    price_usd: float = Field(gt=0.0)
    observed_at: AwareDatetime
    fetched_at: AwareDatetime
    freshness: Freshness = Freshness.FRESH
    age_s: float = Field(default=0.0, ge=0.0)


class CrossValidationReport(Frozen):
    """Comparison of a reference price against secondary exchange spot venues."""

    coin_id: str
    reference_venue: str = "coingecko"
    reference_price_usd: float = Field(gt=0.0)
    quotes: list[VenueQuote] = Field(default_factory=list)
    consensus_price_usd: float = Field(gt=0.0)
    max_spread_pct: float = Field(default=0.0, ge=0.0)
    divergent_venues: list[str] = Field(default_factory=list)
    is_consistent: bool = True
    warnings: list[str] = Field(default_factory=list)


class Observation(Frozen):
    """Structured provenance artifact emitted alongside tool text output."""

    source: str
    endpoint: str
    entity: str
    observed_at: AwareDatetime
    fetched_at: AwareDatetime
    freshness: Freshness = Freshness.FRESH
    age_s: float = Field(default=0.0, ge=0.0)
    metrics: dict[str, float | int | str | None] = Field(default_factory=dict)
    urls: list[str] = Field(default_factory=list)
    cross_validation: CrossValidationReport | None = None


class IndicatorSnapshot(Frozen):
    """Typed technical-indicator snapshot with explicit as-of provenance."""

    coin_id: str
    timeframe: Literal["daily", "4h"]
    as_of: AwareDatetime
    source: str = "coingecko"
    freshness: Freshness = Freshness.FRESH
    age_s: float = Field(default=0.0, ge=0.0)
    candles: int
    price: float
    sma20: float | None = None
    sma50: float | None = None
    sma200: float | None = None
    ema12: float | None = None
    ema26: float | None = None
    rsi14: float | None = None
    rsi14_state: str = "N/A"
    macd: float | None = None
    macd_signal: float | None = None
    macd_histogram: float | None = None
    macd_state: str = "N/A"
    bollinger_upper: float | None = None
    bollinger_middle: float | None = None
    bollinger_lower: float | None = None
    percent_b: float | None = None
    realized_volatility_annualized_pct: float | None = None
    max_drawdown_pct: float | None = None
    atr14: float | None = None
    atr14_pct_of_price: float | None = None
    nearest_support: float | None = None
    nearest_resistance: float | None = None
    trend: str = "N/A"


class ConfidenceReport(Frozen):
    """Auditable confidence assessment derived from grounding and provenance."""

    level: ConfidenceLevel
    score: Unit
    grounding_score: Unit | None = None
    observation_count: int = Field(default=0, ge=0)
    stale_observations: int = Field(default=0, ge=0)
    max_age_s: float = Field(default=0.0, ge=0.0)
    reasons: list[str] = Field(default_factory=list)


class AlertRule(Frozen):
    """Deterministic threshold rule evaluated against market/indicator snapshots."""

    rule_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    coin_id: CoinId
    operator: AlertOperator
    threshold: float
    cooldown_s: PositiveInt = 3600
    enabled: bool = True
    label: str = ""


class AlertEvent(Frozen):
    """Emitted when an AlertRule condition is satisfied."""

    rule_id: str
    coin_id: str
    operator: AlertOperator
    threshold: float
    observed_value: float
    triggered_at: AwareDatetime
    summary: str
    cross_validation: CrossValidationReport | None = None


class DigestSchedule(Frozen):
    """Configuration for periodic automated research digest runs."""

    digest_id: str = Field(
        default="daily-digest",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    query: str = Field(min_length=3, max_length=2000)
    interval_s: PositiveInt = 86400
    enabled: bool = True


class DeliveryReceipt(Frozen):
    """Audit record of an outbound notification delivery attempt."""

    channel: str
    target: str
    delivered: bool
    chunks_sent: int = Field(default=0, ge=0)
    error: str | None = None


class RouteQuery(BaseModel):
    """Result of intent classification."""

    category: Category = Field(description="Kategori pertanyaan yang paling sesuai.")
    reasoning: str = Field(description="Alasan singkat pemilihan kategori.")


class Source(Frozen):
    """A verified source collected from a tool call."""

    title: str
    url: str

    def __init__(self, title: str = _UNSET, url: str = _UNSET, **data: Any) -> None:
        if title is not _UNSET:
            data.setdefault("title", title)
        if url is not _UNSET:
            data.setdefault("url", url)
        super().__init__(**data)


class GroundingReport(Frozen):
    """How many numbers in the report were found in tool output (heuristic)."""

    total: int = Field(description="Number of checkable numbers found in the report.")
    verified: int = Field(description="How many matched tool output (within rounding).")
    score: float | None = Field(description="verified / total, or null if no numbers.")
    ungrounded: list[str] = Field(
        default_factory=list,
        description="Numbers that could not be verified.",
    )

    def __init__(
        self,
        total: int = _UNSET,
        verified: int = _UNSET,
        score: float | None = _UNSET,
        ungrounded: list[str] = _UNSET,
        **data: Any,
    ) -> None:
        if total is not _UNSET:
            data.setdefault("total", total)
        if verified is not _UNSET:
            data.setdefault("verified", verified)
        if score is not _UNSET:
            data.setdefault("score", score)
        if ungrounded is not _UNSET:
            data.setdefault("ungrounded", ungrounded)
        super().__init__(**data)


class AgentResult(BaseModel):
    """Final output of one research run."""

    model_config = ConfigDict(extra="forbid")

    query: str
    category: str
    routing_reason: str
    analysis_time: str
    report: str
    tools_used: list[str] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    latency_seconds: float = 0.0
    request_id: str = ""
    model: str = ""
    grounding: GroundingReport | None = None
    compliance_flags: list[str] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    cross_validations: list[CrossValidationReport] = Field(default_factory=list)
    confidence: ConfidenceReport | None = None

    def __init__(
        self,
        query: str = _UNSET,
        category: str = _UNSET,
        routing_reason: str = _UNSET,
        analysis_time: str = _UNSET,
        report: str = _UNSET,
        tools_used: list[str] = _UNSET,
        sources: list[Source] = _UNSET,
        warnings: list[str] = _UNSET,
        latency_seconds: float = _UNSET,
        request_id: str = _UNSET,
        model: str = _UNSET,
        grounding: GroundingReport | None = _UNSET,
        compliance_flags: list[str] = _UNSET,
        observations: list[Observation] = _UNSET,
        cross_validations: list[CrossValidationReport] = _UNSET,
        confidence: ConfidenceReport | None = _UNSET,
        **data: Any,
    ) -> None:
        positional = {
            "query": query,
            "category": category,
            "routing_reason": routing_reason,
            "analysis_time": analysis_time,
            "report": report,
            "tools_used": tools_used,
            "sources": sources,
            "warnings": warnings,
            "latency_seconds": latency_seconds,
            "request_id": request_id,
            "model": model,
            "grounding": grounding,
            "compliance_flags": compliance_flags,
            "observations": observations,
            "cross_validations": cross_validations,
            "confidence": confidence,
        }
        for key, val in positional.items():
            if val is not _UNSET:
                data.setdefault(key, val)
        super().__init__(**data)

    def to_dict(self) -> dict[str, Any]:
        """Serialize result to a plain dictionary (backward-compatible helper)."""
        return self.model_dump(mode="json")

    def render_markdown(self, include_header: bool = True) -> str:
        """Report body + data-quality notes + tool-verified sources + disclaimer."""
        parts: list[str] = []
        if include_header:
            parts += [
                "# ALPHA INTELLIGENCE REPORT",
                f"**Waktu Analisis:** {self.analysis_time}  \n"
                f"**Mode Riset:** {self.category}",
                "",
            ]
        parts.append(self.report.strip())
        if self.warnings:
            parts += ["", "## Catatan Kualitas Data"]
            parts += [f"- {w}" for w in self.warnings]
        g = self.grounding
        if g is not None and g.total:
            parts += [
                "",
                "## Verifikasi Numerik",
                f"{g.verified} dari {g.total} angka dalam laporan cocok dengan"
                f"data tool (skor {g.score:.0%}).",
            ]
            if g.ungrounded:
                ungrounded_fmt = ", ".join(f"`{x}`" for x in g.ungrounded)
                parts.append(f"Tidak terverifikasi: {ungrounded_fmt}")
        if self.cross_validations:
            parts += ["", "## Validasi Silang Multi-Venue"]
            for cv in self.cross_validations:
                status = "KONSISTEN" if cv.is_consistent else "DIVERGEN"
                venues = (
                    ", ".join(f"{q.venue} (${q.price_usd:,.2f})" for q in cv.quotes)
                    or "tidak ada venue sekunder"
                )
                parts.append(
                    f"- **{cv.coin_id}**: {status} | Ref ({cv.reference_venue}): "
                    f"${cv.reference_price_usd:,.2f} | Konsensus: "
                    f"${cv.consensus_price_usd:,.2f} | Spread maks: "
                    f"{cv.max_spread_pct:.2f}% | Pembanding: {venues}"
                )
        if self.confidence is not None:
            parts += [
                "",
                "## Tingkat Kepercayaan Data",
                f"**Level:** {self.confidence.level.value} "
                f"(skor {self.confidence.score:.0%}, "
                f"{self.confidence.observation_count} observasi)",
            ]
            for reason in self.confidence.reasons:
                parts.append(f"- {reason}")
        if self.compliance_flags:
            parts += ["", "## Catatan Kepatuhan"]
            parts += [f"- {flag}" for flag in self.compliance_flags]
        parts += ["", "## Sumber (Terverifikasi dari Tool)"]
        if self.sources:
            parts += [
                f"{i}. [{s.title}]({s.url})" for i, s in enumerate(self.sources, 1)
            ]
        else:
            parts.append("_Tidak ada sumber eksternal yang berhasil diambil._")
        parts += ["", "---", DISCLAIMER]
        return "\n".join(parts)
