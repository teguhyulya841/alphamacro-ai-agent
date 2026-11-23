# Changelog

## 2.0.0 - Verifiable analytics

- **Quant engine:** RSI, MACD, SMA/EMA, Bollinger, ATR, swing support/resistance, volatility and
  drawdown computed by code from real OHLC/price data (`get_technical_indicators` tool). RSI is
  checked against a published reference example.
- **Numeric grounding:** every number in a report is checked against tool output; a score and the
  list of unverified numbers are attached, with a warning below `GROUNDING_MIN_SCORE`.
- **Compliance guardrails:** secrets (API keys, private keys, seed phrases) are blocked before any
  third-party call; PII is redacted; prompt-injection patterns flagged; risky output language
  (guarantees, personal buy/sell orders, price targets) flagged.
- **Tamper-evident audit log:** SHA-256 hash-chained JSONL, pseudonymised users, query text not
  stored by default, `alphamacro-audit verify`.
- **REST API (FastAPI):** API-key auth (hashes only), per-key rate limit, concurrency cap, request
  IDs, OpenAPI docs, deterministic `/v1/indicators/{coin}` endpoint, `alphamacro-keygen`.
- Docker compose with `bot` and `api` services.

## 1.0.0

- Restructured into an installable `src/` package with CLI and Telegram bot.
