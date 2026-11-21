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

## How changes ship

Railway deploys whatever lands on `main`, so nothing goes to `main` directly.

1. Work on a new branch and push the branch.
2. GitHub Actions (`.github/workflows/tests.yml`) runs every test, including the
   database tests against a throwaway Postgres with all migrations applied. A
   skipped test counts as a failure. Fix and push again until the run is green.
3. Merge to `main` (fast-forward) only when CI passed on that exact commit with
   no skipped tests. Branch protection on `main` requires the CI check. Never
   force-push.
4. After every deploy, check `/healthz` and the worker log. If either fails,
   revert the merge (a revert commit, through CI like any change), confirm the
   site is healthy again, and tell the owner.

## What needs the owner's approval (owner's policy, 2026-11-21)

Do without asking, and report afterwards:
- push branches; merge to `main` when CI is green on that exact commit, with no
  skipped tests
- deploys, and migrations that only add tables, columns or indexes
- bug fixes, tests, docs, and follow-up work within a brief the owner has given
- restarting jobs, running live checks and case checks within the ledger limits

Always ask first:
- anything that costs money or changes a paid plan (the no-spending rule stands)
- changing secrets or settings in Railway, Clerk or Cloudflare
- deleting data, or a migration that alters or drops anything existing
- changing who can use the site, or enabling anything public
- adding an outside data source, or anything that touches a provider's terms
- changes to the Privacy or Terms pages or the two fixed report sections
  (recovery scams, IC3 checklist)
- contacting anyone outside the project
- changing the design rules in this file
- force-pushing: never

When unsure which list something belongs to, ask.

At the end of each working session, save a summary to `~/Downloads/` named
`taw-review-<date>-summary.md`: what was merged, what changed for users, anything
that failed or was reverted, and anything waiting on the owner.

## Review files for the owner

Save every review file written for the owner in `~/Downloads/`, named
`taw-review-` plus the date and a short title, e.g.
`~/Downloads/taw-review-2026-11-20-report-wording.md`. These files live outside
the repo and are never committed.

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
    taw/main.py          mounts the API, the web pages and their security headers
    taw/api/             /api/v1 routes, behind Clerk sign-in (taw/auth.py) and limits (taw/guard.py)
    taw/web/             server-rendered pages: a client of the API, never the database
    taw/lookups.py       private lookups, their jobs, transaction matching runs
    taw/worker/          job loop
    taw/adapters/        chains/, labels/, prices/ (to be built)
    taw/report/          builder, canonical JSON, hash, templates
    tests/cases/         five ground-truth cases from DOJ forfeiture complaints
