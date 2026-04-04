# MCP Developer Portal

Backend service for the MCP Platform developer portal.

## Features

- **API key issuance** — users sign up and receive an `mcp_` prefixed API key
- **Call-based billing** via Stripe
  - Free tier: 1,000 calls/month (no credit card required)
  - Pro tier: unlimited calls/month (flat rate Stripe subscription)
- **Gateway check endpoint** — called by the MCP gateway on every inbound request to enforce rate limits
- **Usage dashboard** — per-key call counts queryable by month

## Architecture

```
Client → MCP Gateway → POST /v1/gateway/check (billing service)
                     ↓
              Forward if allowed=True
              Return 429 if allowed=False
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DYNAMODB_TABLE_NAME` | yes | DynamoDB table for portal data |
| `AWS_REGION` | yes | AWS region |
| `JWT_SECRET_KEY` | yes (non-dev) | JWT signing secret |
| `STRIPE_SECRET_KEY` | yes (billing) | Stripe API secret key |
| `STRIPE_WEBHOOK_SECRET` | yes (billing) | Stripe webhook signing secret |
| `STRIPE_PRO_PRICE_ID` | yes (billing) | Stripe Price ID for Pro tier |
| `FREE_TIER_MONTHLY_CALLS` | no | Default: 1000 |

## Local Development

```bash
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

## DynamoDB Table Schema

| PK | SK | Entity | Notes |
|----|-----|--------|-------|
| `APIKEY#<key>` | `META` | ApiKey | Key record with tier |
| `USAGE#<key>#<YYYY-MM>` | `COUNT` | Usage | Atomic call counter |
| `USER#<user_id>` | `SUBSCRIPTION` | Subscription | Stripe subscription |

GSI1: `USER#<user_id>` / `APIKEY#<key>` — list keys per user
