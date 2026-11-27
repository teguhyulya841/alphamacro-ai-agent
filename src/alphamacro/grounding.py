"""Numeric grounding verification & provenance-based confidence scoring.

Grounding checks that numbers in the report appear in raw tool outputs or structured
Observation artifacts (within rounding tolerance). Confidence combines numeric
grounding, observation freshness/staleness, and multi-venue spot price consistency.
"""

from __future__ import annotations

import bisect
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from alphamacro.models import (
    ConfidenceLevel,
    ConfidenceReport,
    CrossValidationReport,
    Freshness,
    GroundingReport,
    Observation,
)

_MONTHS = (
    "januari|februari|maret|april|mei|juni|juli|agustus|september|oktober|november|"
    "desember|january|february|march|may|june|july|august|october|december|"
    "jan|feb|mar|apr|jun|jul|agu|aug|sep|sept|okt|oct|nov|des|dec"
)
_URL_RE = re.compile(r"https?://\S+")
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)")
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_TEXT_DATE_RE = re.compile(rf"\b\d{{1,2}}\s+(?:{_MONTHS})\b\.?(?:\s+\d{{4}})?", re.I)
_LIST_MARKER_RE = re.compile(r"^\s*\d+[.)]\s+", re.M)
_NUMBER_RE = re.compile(
    r"""
    (?<![\w.,])
    (?P<cur>[$€£]|Rp\.?\s?|USD\s?)?
    (?P<neg>[-+\u2212])?
    (?P<num>\d{1,3}(?:[.,]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)
    (?P<suffix>\s?(?:%|[KMBT]|(?i:ribu|juta|miliar|triliun|thousand|million|billion|trillion))
        (?![A-Za-z]))?
    """,
    re.X,
)
_DURATION_RE = re.compile(
    r"^\s*(?:hari|minggu|bulan|tahun|jam|menit|detik|kali|days?|weeks?|months?|years?|"
    r"hours?|minutes?|x\b)",
    re.I,
)
_MULTIPLIERS = {
    "k": 1e3, "ribu": 1e3, "thousand": 1e3,
    "m": 1e6, "juta": 1e6, "million": 1e6,
    "b": 1e9, "miliar": 1e9, "billion": 1e9,
    "t": 1e12, "triliun": 1e12, "trillion": 1e12,
}  # fmt: skip

_SINGLE_DOT_THOUSANDS_RE = re.compile(r"^\d{1,3}\.\d{3}$")
_MULTI_DOT_THOUSANDS_RE = re.compile(r"^\d{1,3}(?:\.\d{3}){1,}$")
_MULTI_COMMA_THOUSANDS_RE = re.compile(r"^\d{1,3}(?:,\d{3}){1,}$")
_US_DECIMAL_RE = re.compile(r"^\d+(?:,\d{3})*\.\d+$")
_ID_DECIMAL_RE = re.compile(r"^\d+(?:\.\d{3})*,\d+$")
_PLAIN_INT_RE = re.compile(r"^\d+$")


@dataclass(frozen=True)
class NumberMention:
    raw: str
    candidates: tuple[tuple[float, float], ...]  # (absolute value, rounding half-unit)
    countable: bool


def _parse_number(num: str) -> list[tuple[float, int]]:
    """Return (value, decimals) readings for US and Indonesian formats."""
    cleaned = num.strip().replace(" ", "")
    if not cleaned:
        return []

    has_dot = "." in cleaned
    has_comma = "," in cleaned

    try:
        if has_dot and has_comma:
            if _US_DECIMAL_RE.match(cleaned):
                int_part, frac_part = cleaned.rsplit(".", 1)
                val = float(int_part.replace(",", "") + "." + frac_part)
                return [(val, len(frac_part))]
            if _ID_DECIMAL_RE.match(cleaned):
                int_part, frac_part = cleaned.rsplit(",", 1)
                val = float(int_part.replace(".", "") + "." + frac_part)
                return [(val, len(frac_part))]

            last_dot = cleaned.rfind(".")
            last_comma = cleaned.rfind(",")
            sep = "." if last_dot > last_comma else ","
            int_part, frac_part = cleaned.rsplit(sep, 1)
            clean_int = int_part.replace(".", "").replace(",", "")
            clean_frac = frac_part.replace(".", "").replace(",", "")
            candidate = f"{clean_int}.{clean_frac}" if clean_frac else clean_int
            return [(float(candidate), len(clean_frac))]

        if has_comma:
            if _MULTI_COMMA_THOUSANDS_RE.match(cleaned):
                return [(float(cleaned.replace(",", "")), 0)]
            parts = cleaned.split(",")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                if len(parts[1]) == 3:
                    return [
                        (float(f"{parts[0]}.{parts[1]}"), 3),
                        (float(f"{parts[0]}{parts[1]}"), 0),
                    ]
                if len(parts[1]) in (1, 2):
                    return [(float(f"{parts[0]}.{parts[1]}"), len(parts[1]))]
            return [(float(cleaned.replace(",", "")), 0)]

        if has_dot:
            if _SINGLE_DOT_THOUSANDS_RE.match(cleaned):
                int_part, frac_part = cleaned.split(".", 1)
                return [
                    (float(cleaned), 3),
                    (float(f"{int_part}{frac_part}"), 0),
                ]

            if _MULTI_DOT_THOUSANDS_RE.match(cleaned):
                return [(float(cleaned.replace(".", "")), 0)]

            parts = cleaned.split(".")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                return [(float(cleaned), len(parts[1]))]
        return [(float(cleaned), 0)]
    except ValueError:
        return []


def _clean(text: str) -> str:
    for pattern in (_MD_LINK_RE, _URL_RE, _ISO_DATE_RE, _TEXT_DATE_RE, _TIME_RE):
        text = pattern.sub(" ", text)
    return _LIST_MARKER_RE.sub("", text)


def extract_mentions(text: str) -> list[NumberMention]:
    text = _clean(text)
    mentions: list[NumberMention] = []
    for match in _NUMBER_RE.finditer(text):
        suffix = (match.group("suffix") or "").strip()
        after = text[match.end() : match.end() + 1]
        if not suffix and after.isalpha():
            continue  # e.g. "10Y", "5G"
        multiplier = _MULTIPLIERS.get(suffix.lower(), 1.0)
        if suffix == "%":
            multiplier = 1.0
        readings = _parse_number(match.group("num"))
        candidates = tuple(
            (abs(value) * multiplier, 0.5 * 10 ** (-decimals) * multiplier)
            for value, decimals in readings
        )

        plain_int = (
            len(readings) == 1
            and readings[0][1] == 0
            and not any(ch in match.group("num") for ch in ".,")
        )
        countable = True
        if plain_int and not (suffix or match.group("cur")):
            value = readings[0][0]
            if (
                1900 <= value <= 2100
                or value <= 10
                or _DURATION_RE.match(text[match.end() :])
            ):
                countable = False
        elif (
            not suffix
            and not match.group("cur")
            and _DURATION_RE.match(text[match.end() :])
        ):
            countable = False
        mentions.append(NumberMention(match.group(0).strip(), candidates, countable))
    return mentions


def _source_values(
    tool_outputs: Sequence[str],
    observations: Sequence[Observation] = (),
) -> list[float]:
    values: list[float] = []
    for output in tool_outputs:
        for mention in extract_mentions(output):
            values.extend(value for value, _ in mention.candidates)
    for obs in observations:
        for metric_val in obs.metrics.values():
            if isinstance(metric_val, int | float) and not isinstance(metric_val, bool):
                num = abs(float(metric_val))
                if math.isfinite(num):
                    values.append(num)
    return sorted(values)


def _matches(
    sorted_values: list[float], target: float, half_unit: float, rel_tol: float
) -> bool:
    window = max(half_unit, rel_tol * target) * 1.0000001
    index = bisect.bisect_left(sorted_values, target - window)
    return index < len(sorted_values) and sorted_values[index] <= target + window


def verify_numeric_grounding(
    report: str,
    tool_outputs: Sequence[str],
    *,
    observations: Sequence[Observation] = (),
    rel_tol: float = 0.001,
    max_listed: int = 10,
) -> GroundingReport:
    """Compare numbers in ``report`` with tool outputs and Observation metrics."""
    mentions = [m for m in extract_mentions(report) if m.countable]
    source = _source_values(tool_outputs, observations=observations)

    verified = 0
    ungrounded: list[str] = []
    for mention in mentions:
        if any(
            _matches(source, value, half, rel_tol) for value, half in mention.candidates
        ):
            verified += 1
        elif mention.raw not in ungrounded:
            ungrounded.append(mention.raw)

    total = len(mentions)
    return GroundingReport(
        total=total,
        verified=verified,
        score=(verified / total) if total else None,
        ungrounded=ungrounded[:max_listed],
    )


def build_staleness_warnings(observations: Sequence[Observation]) -> list[str]:
    """Generate human-readable warnings when stale cached data was used."""
    warnings: list[str] = []
    for obs in observations:
        if obs.freshness is Freshness.STALE:
            warnings.append(
                f"Data {obs.source} ({obs.entity}) diambil dari cache berstatus STALE "
                f"(usia {obs.age_s:.0f} detik) karena pembaruan langsung tertunda."
            )
        elif obs.freshness is Freshness.EXPIRED:
            warnings.append(
                f"Data {obs.source} ({obs.entity}) telah melewati batas kedaluwarsa "
                f"(usia {obs.age_s:.0f} detik)."
            )
    return warnings


def assess_confidence(
    *,
    grounding: GroundingReport,
    observations: Sequence[Observation],
    cross_validations: Sequence[CrossValidationReport] = (),
    grounding_min_score: float = 0.8,
) -> ConfidenceReport:
    """Compute a deterministic ConfidenceReport from grounding and provenance."""
    obs_count = len(observations)
    stale_count = sum(1 for o in observations if o.freshness is not Freshness.FRESH)
    max_age_s = max((o.age_s for o in observations), default=0.0)
    reasons: list[str] = []

    if obs_count == 0:
        reasons.append("Tidak ada observasi data dari tool eksternal.")
        return ConfidenceReport(
            level=ConfidenceLevel.LOW,
            score=0.25,
            grounding_score=grounding.score,
            observation_count=0,
            stale_observations=0,
            max_age_s=0.0,
            reasons=reasons,
        )

    score = grounding.score if grounding.score is not None else 0.85
    if grounding.score is None:
        reasons.append("Laporan bersifat kualitatif tanpa klaim numerik spesifik.")
    elif grounding.score < grounding_min_score:
        reasons.append(
            f"Skor verifikasi numerik ({grounding.score:.0%}) berada di bawah "
            f"ambang minimum ({grounding_min_score:.0%})."
        )
    else:
        reasons.append(
            f"{grounding.verified} dari {grounding.total} angka terverifikasi "
            f"terhadap sumber tool ({grounding.score:.0%})."
        )

    if stale_count > 0:
        score = max(0.0, score - 0.2 * stale_count)
        reasons.append(
            f"{stale_count} dari {obs_count} observasi menggunakan data STALE "
            f"(usia maks {max_age_s:.0f} detik)."
        )

    divergent_reports = [cv for cv in cross_validations if not cv.is_consistent]
    consistent_reports = [
        cv for cv in cross_validations if cv.is_consistent and cv.quotes
    ]
    if divergent_reports:
        worst = max(cv.max_spread_pct for cv in divergent_reports)
        score = max(0.0, score - 0.25)
        reasons.append(
            f"Terdeteksi divergensi harga antar-venue hingga {worst:.2f}% "
            f"pada {len(divergent_reports)} aset."
        )
    elif consistent_reports:
        total_venues = sum(len(cv.quotes) for cv in consistent_reports)
        reasons.append(
            f"Harga tervalidasi silang terhadap {total_venues} kutipan exchange "
            "dengan spread dalam batas normal."
        )

    clamped = round(min(1.0, max(0.0, score)), 4)

    if grounding.score is not None and grounding.score < 0.5:
        level = ConfidenceLevel.LOW
    elif (
        stale_count > 0
        or bool(divergent_reports)
        or (grounding.score is not None and grounding.score < grounding_min_score)
    ):
        level = ConfidenceLevel.MEDIUM if clamped >= 0.5 else ConfidenceLevel.LOW
    elif clamped >= 0.8:
        level = ConfidenceLevel.HIGH
    elif clamped >= 0.5:
        level = ConfidenceLevel.MEDIUM
    else:
        level = ConfidenceLevel.LOW

    return ConfidenceReport(
        level=level,
        score=clamped,
        grounding_score=grounding.score,
        observation_count=obs_count,
        stale_observations=stale_count,
        max_age_s=round(max_age_s, 2),
        reasons=reasons,
    )
