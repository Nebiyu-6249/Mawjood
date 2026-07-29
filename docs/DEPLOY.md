# DEPLOY

How Mawjood is deployed, and to where.

> **Status: stub.** The target cloud is a deliberately deferred decision — see
> `CLAUDE.md` decision 5. This file is written in full in Phase 4, once that
> decision is made. What follows is what Phase 0 has already guaranteed.

## What is already true

**The image is portable.** `Dockerfile` produces a plain OCI image with no
cloud-specific SDKs, no baked configuration and no assumed region. It runs
unchanged wherever a container runs.

**Configuration is entirely environmental.** Every setting is read by
`mawjood/config.py` with the `MAWJOOD_` prefix. See `.env.example`. Nothing is
read from a committed file — including the database URL, which `alembic.ini`
deliberately leaves blank and `env.py` injects from settings.

**Residency is enforced in code, not documentation:**
- `MAWJOOD_DATA_RESIDENCY` accepts `gcc`, `eu` or `india`. There is no US member,
  so a US-only deployment is not representable.
- `MAWJOOD_REGION` is rejected at startup if it looks like a US region code
  (`us-east-1`, `eastus`, `westus3`, `northamerica-northeast1`, …).

Both rules are covered by `tests/test_config.py`.

**The process runs unprivileged.** UID 10001, no login shell, no root.

## To be written in Phase 4

- Chosen provider, region and the reasoning.
- Managed PostgreSQL: sizing, TLS enforcement, encryption at rest, backup policy.
- Secret store binding — `services/secrets.py` ships an env backend behind an
  interface; the managed-store implementation lands with the provider choice.
- TLS termination and the ingress path.
- Migration strategy on deploy, and the rollback path.
- Log shipping with 90-day retention.
- Scaling and the readiness/liveness probe configuration.
