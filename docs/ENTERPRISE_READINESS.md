# Enterprise readiness: an honest assessment

This document separates what the code already provides from what a regulated financial
institution will still require. Nothing here is a guarantee that any company will buy the product.

## Controls implemented in this repository

| Concern | Control | Where |
|---|---|---|
| Fabricated numbers | Every number in a report is checked against raw tool output; score + unverified list attached | `grounding.py` |
| Unreliable technical analysis | Indicators computed by deterministic, tested code, not by the LLM | `quant/`, `tools/technical.py` |
| Secret leakage to third parties | API keys, private keys and seed phrases are blocked before any LLM call | `compliance.py` |
| Personal data | Emails, phones, cards (Luhn) and 16-digit IDs redacted from prompts | `compliance.py` |
| Risky financial language | Guarantee wording, personal buy/sell orders, price targets flagged | `compliance.py` |
| Accountability | Hash-chained audit log, pseudonymised users, verifiable offline | `audit.py` |
| Integration | Authenticated REST API with rate limiting and OpenAPI contract | `api/` |
| Operations | Typed config, secret-redacting logs, JSON logs, non-root container | `config.py`, `Dockerfile` |

Known limits of these controls: grounding checks that a number *exists* in tool output, not that it
is used correctly; guardrails are pattern-based; audit-log tail truncation is only detectable if the
latest hash is anchored elsewhere.

## Gaps before a regulated buyer will sign

1. **Data and model licensing (blocking).** Free/demo tiers of CoinGecko, Tavily and Gemini are not
   automatically licensed for commercial redistribution. Obtain commercial plans, and confirm
   data-retention and "no training on your data" terms for the LLM tier you use.
2. **Authoritative macro data.** Macro answers currently rely on web search. A finance buyer expects
   primary sources (e.g. FRED, central-bank and statistics-agency APIs) as dedicated tools, with
   release dates, so CPI/rates numbers are citable.
3. **Regulatory and legal review.** Crypto and investment-communication rules vary by country
   (in Indonesia, OJK oversight and personal-data law). Get legal sign-off on disclaimers, the
   "information, not advice" positioning, record retention and cross-border data transfer.
4. **Security assurance.** Independent penetration test, dependency/SAST scanning in CI, secrets in
   a vault rather than `.env`, SSO/RBAC instead of shared API keys, and eventually SOC 2 or
   ISO 27001 evidence.
5. **Reliability.** SLA, multi-provider LLM failover, shared rate-limit/cache store (Redis),
   horizontal deployment, metrics/alerting, load tests. Current limiter and cache are in-process.
6. **Model risk management.** A versioned evaluation set run in CI, regression tracking for
   grounding and compliance flags, human review workflow, and drift monitoring.
7. **Support and contracts.** DPA, SLA, incident process, pricing, named support contacts.

## Realistic go-to-market

A global top-tier institution typically needs 6-18 months of vendor due diligence and will ask for
every item above. A more realistic path is to start with pilots at crypto exchanges, fintechs,
brokers and research desks (especially in Indonesia), position the product as a
"verifiable research copilot / white-label API" rather than a chatbot, and use pilot results
(grounding scores, audit logs, time saved) as evidence for larger buyers.

## Suggested next engineering steps

1. FRED / central-bank macro tools with release-date metadata.
2. Evaluation harness with a fixed question set, run in CI.
3. Redis-backed rate limiting and caching; Prometheus metrics endpoint.
4. SSO (OIDC) and per-tenant keys with usage metering.
5. Multi-asset watchlists and scheduled briefings (Telegram/email).
6. Backtesting module so signals can be evaluated against history.
