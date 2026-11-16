# TraceAWallet

A free web app, with an API behind it, for crypto fraud victims and investigators.
A signed-in user enters the wallet address the money was sent to and gets a
one-page report that helps an investigation. Site: traceawallet.com.

The owner is not a professional developer. Explain what you are about to do in
plain language, make small commits, and never ask him to edit code by hand when
you can do it.

## Design rules (do not break these)

1. The API is the only way into the data. The website is its first client.
2. Observations and assessments are separate records. Phase 1 stores no assessments.
3. Every raw provider response is saved to the bucket, keyed by its sha256, and
   indexed in `raw_response`.
4. Every record carries its source. The `source` table holds the license and a
   "resale permitted" flag.
5. History is append-only. Corrections supersede; nothing is silently deleted.
   The one exception is `lookup_redact()` for erasure requests.
6. The `lookup` table (who searched for what) is private and never feeds anything
   public. Reports read `lookup_count`, never `lookup`.
7. Each chain and each label source sits behind one adapter interface.
8. Cache results by chain and address with a freshness window; record the block
   height each snapshot was taken at. Rate-limit per account.
9. A label from a third party is always worded as that party's claim.

No LLM-written text in reports. Report prose comes from templates over structured data.
No secrets in the repo. Settings come from environment variables (see `env.example`).

## Stack

- Railway: `web` and `worker` services from this repo, Postgres, a bucket.
  `start.sh` reads `SERVICE_ROLE` to decide which one to run.
- Postgres is also the job queue (`job` table, `FOR UPDATE SKIP LOCKED`). No Redis.
- FastAPI with server-rendered Jinja pages. psycopg 3 with plain SQL, no ORM.
- Clerk for sign-in. Cloudflare Turnstile on the lookup form.
- Schema changes are new numbered files in `migrations/`. Never edit an applied
  migration. `scripts/migrate.py` runs them when the web service starts.

## Phase 1 scope

Chains: Ethereum and other main EVM chains, Tron, Bitcoin.
Data sources: Etherscan API V2 (EVM), TronGrid (Tron), an Esplora-style API
(Bitcoin), DefiLlama (prices), GoPlus (flag, worded "flagged by GoPlus"),
OFAC SDN and Israel NBCTF lists, stablecoin freeze status from the contracts,
a hand-built exchange wallet file. Moralis may be added later behind the same
adapter interface.

Out of scope: multi-hop tracing, PDF export, alerts, public API keys, file uploads.

## Layout

    migrations/          numbered SQL files
    scripts/migrate.py   migration runner
    taw/main.py          web app (will split into api/ and web/)
    taw/worker/          job loop
    taw/adapters/        chains/, labels/, prices/ (to be built)
    taw/report/          builder, canonical JSON, hash, templates (to be built)
    tests/cases/         five ground-truth cases from DOJ forfeiture complaints
