# AlphaMacro-Crypto

**AI Macroeconomics & Cryptocurrency Research Engine with Verifiable Outputs**

AlphaMacro-Crypto is a research orchestration project that separates quantitative calculations from LLM narrative generation. Market snapshots, technical indicator snapshots, cross-venue spot checks, numeric grounding checks, and confidence assessments are computed in Python rather than relying solely on model text generation.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Implemented Features](#implemented-features)
- [Current Limitations & Scope](#current-limitations--scope)
- [Project Structure](#project-structure)
- [Local Quickstart](#local-quickstart)
- [Running with Docker Compose](#running-with-docker-compose)
- [REST API Endpoints](#rest-api-endpoints)
- [Verification](#verification)
- [Disclaimer](#disclaimer)

---

## Architecture Overview

```text
                        ┌────────────────────────────────────┐
                        │     CLI  |  FastAPI  |  Worker     │
                        └─────────────────┬──────────────────┘
                                          ▼
                        ┌────────────────────────────────────┐
                        │  Input Guard & Intent Router       │
                        │  (Secret Blocking / PII Redaction) │
                        └─────────────────┬──────────────────┘
                                          ▼
         ┌────────────────────────────────┼────────────────────────────────┐
         ▼                                ▼                                ▼
┌─────────────────┐            ┌─────────────────────┐          ┌─────────────────────┐
│  Tavily Search  │            │  CoinGecko Async    │          │  Multi-Venue Spot   │
│  (Macro & News) │            │  + CachingFeed      │          │  Binance & Coinbase │
└────────┬────────┘            └──────────┬──────────┘          └──────────┬──────────┘
         │                                │                                │
         │                     ┌──────────▼──────────┐                     │
         │                     │  Quant Engine       │                     │
         │                     │  Indicators &       │                     │
         │                     │  Snapshot Builder   │                     │
         │                     └──────────┬──────────┘                     │
         └────────────────────────────────┼────────────────────────────────┘
                                          ▼
                        ┌────────────────────────────────────┐
                        │  Verification & Audit Layer        │
                        │  • Observation Provenance          │
                        │  • Numeric Grounding Check         │
                        │  • Confidence Scoring (H/M/L)      │
                        │  • Compliance & JSONL AuditLog     │
                        └─────────────────┬──────────────────┘
                                          ▼
                        ┌────────────────────────────────────┐
                        │  Markdown / JSON / Telegram Output │
                        └────────────────────────────────────┘
```

---

## Implemented Features

- **Async Core & Caching (M1):** Asynchronous HTTP clients built on `httpx` with retry handling, `CachingFeed` supporting stale-while-revalidate and stale-if-error policies, and state backends across Memory, SQLite, and Redis.
- **Quant Engine (M2):** Pure Python technical indicator and snapshot modules (`src/alphamacro/quant/indicators.py` and `src/alphamacro/quant/snapshot.py`) covering moving averages, momentum/oscillator metrics, volatility/drawdown metrics, and support/resistance levels.
- **Provenance & Grounding (M3a):** Structured `Observation` artifacts with timestamps and freshness metadata, `verify_numeric_grounding()` against tool outputs and observation metrics, and `ConfidenceReport` (`HIGH`, `MEDIUM`, `LOW`).
- **Multi-Venue Cross-Validation (M3b):** Concurrent spot price checks against Binance and Coinbase via `MultiVenueValidator` to compute consensus prices, spread percentages, and divergence warnings.
- **Alerts, Digest & Telegram Delivery (M4):** Threshold alert evaluation (`ResearchScheduler`), scheduled digest execution via `AlphaMacroEngine.arun()`, and outbound Telegram Bot API delivery with automatic long-message splitting and HTML/plain-text fallback.

---

## Current Limitations & Scope

- **External public APIs:** Market data, spot quotes, web search, and LLM calls depend on third-party APIs (CoinGecko, Binance, Coinbase, Tavily, Google Gemini), which are subject to rate limits, network latency, and upstream availability.
- **Research latency:** Multi-tool research runs (`FULL_RESEARCH`) can take tens of seconds depending on tool calls and upstream response times. This project is designed for research and scheduled digests, not low-latency trade execution.
- **State & integration testing:** Automated tests currently cover unit, contract, and mocked integration scenarios. Live multi-instance Redis and end-to-end Telegram delivery should be verified in your target deployment environment.

---

## Project Structure

```text
src/alphamacro/
├── api/
│   ├── app.py              # FastAPI endpoints (/healthz, /readyz, /v1/analyze, /v1/indicators, /v1/alerts)
│   ├── auth.py             # SHA-256 X-API-Key verification
│   └── main.py             # ASGI entrypoint for Uvicorn
├── delivery/
│   └── telegram.py         # Async Telegram Bot delivery, formatting & message chunking
├── infra/
│   ├── caching.py          # CachingFeed with freshness policies
│   ├── http.py             # Async HTTP client & retry helper
│   ├── sync_bridge.py      # Background event loop helper for sync callers
│   └── state/              # Memory, SQLite, and Redis state adapters
├── quant/
│   ├── indicators.py       # Pure technical indicator calculations
│   └── snapshot.py         # Snapshot builder and plain-text formatter
├── telegram_bot/           # Interactive Telegram bot handlers & formatting
├── tools/
│   ├── coingecko.py        # CoinGecko market/history/OHLC client & tools
│   ├── technical.py        # Technical indicator tools
│   └── venues.py           # Binance & Coinbase spot clients & cross-validation
├── cli.py                  # CLI entrypoint
├── compliance.py           # Secret checks, PII redaction & output checks
├── config.py               # Environment configuration models
├── engine.py               # Main research pipeline orchestration
├── grounding.py            # Numeric grounding & confidence assessment
├── models.py               # Pydantic data models
├── scheduler.py            # Alert rule evaluation & digest scheduling
└── worker.py               # Standalone scheduler worker entrypoint
```

---

## Local Quickstart

### 1. Create a virtual environment and install dependencies

```bash
python -m venv .venv

# Windows PowerShell:
.venv\Scripts\Activate.ps1
# Linux/macOS:
source .venv/bin/activate

pip install -e ".[dev]"
```

### 2. Create `.env` from `.env.example`

```powershell
# Windows PowerShell
Copy-Item .env.example .env
```

```bash
# Linux/macOS
cp .env.example .env
```

Fill in `GOOGLE_API_KEY` and `TAVILY_API_KEY` (plus optional Telegram/API settings).

### 3. Run via CLI

```bash
python -m alphamacro.cli "Analisis teknikal Bitcoin timeframe daily dan sentimen makro Fed"
python -m alphamacro.cli --json "Cek harga Ethereum dan spread multi-venue"
```

---

## Running with Docker Compose

A `docker-compose.yml` file is provided with `redis`, `api`, and `scheduler` services.

```bash
docker compose config
docker compose up -d --build
```

Check service status and health:

```powershell
# Windows PowerShell
docker compose ps
Invoke-RestMethod http://localhost:8000/healthz
```

```bash
# Linux/macOS
docker compose ps
curl http://localhost:8000/healthz
```

---

## REST API Endpoints

All `/v1/*` routes use `X-API-Key` authentication against hashes configured in `API_KEY_HASHES`.

| Method   | Endpoint                      | Purpose                                                   |
|----------|-------------------------------|-----------------------------------------------------------|
| `GET`    | `/healthz`                    | Health check                                              |
| `GET`    | `/readyz`                     | Readiness check and version                               |
| `POST`   | `/v1/analyze`                 | Run research query and return structured result           |
| `GET`    | `/v1/indicators/{coin_id}`    | Compute technical indicator snapshot (`timeframe=daily` or `4h`) |
| `GET`    | `/v1/alerts`                  | List alert rules                                          |
| `POST`   | `/v1/alerts`                  | Create or update an alert rule                            |
| `DELETE` | `/v1/alerts/{rule_id}`        | Delete an alert rule                                      |
| `POST`   | `/v1/alerts/check`            | Evaluate active alert rules                               |

---

## Verification

```bash
ruff check src tests
python -m pytest -v
```

---

## Disclaimer

This project is for informational and research purposes only and does not constitute financial or investment advice.
