# LICENCES

Open-source licence inventory for Mawjood.

**Generated 2026-07-31** from installed package metadata:

```bash
uv run python tools/licences.py            # these tables
uv run python tools/licences.py --json     # docs/evidence/licences.json
```

Re-run it after any dependency change. An inventory written once by hand is a
snapshot that rots on the next `uv sync`.

---

## The short answer

**83 third-party packages. No copyleft obligation reaches the Mawjood source.**

Everything in the runtime image is permissive — MIT, BSD, Apache-2.0, PSF — with
one weak-copyleft exception (`certifi`, MPL-2.0) that is used unmodified and
therefore carries no source-disclosure obligation. Nothing is GPL, AGPL, LGPL,
SSPL or BUSL. Nothing has an unidentifiable licence.

Mawjood itself is **proprietary and unpublished**. That matters for the analysis:
the obligations below are the ones that attach to *distributing* the software.
Mawjood is deployed as a hosted service, not shipped to customers, which is the
weaker of the two cases for almost every licence here — and the one place that
distinction would flip (AGPL, which treats network use as distribution) does not
appear.

---

## Runtime — what ships in the image

36 packages, the closure of `pyproject.toml`'s `dependencies`. These are the only
ones whose obligations reach a deployment.

| Licence | Packages |
|---|---|
| MIT | 18 |
| BSD-3-Clause | 11 |
| Apache-2.0 | 3 |
| PSF-2.0 | 1 |
| MIT OR Apache-2.0 | 1 |
| MIT AND PSF-2.0 | 1 |
| MPL-2.0 | 1 |

| Package | Version | Licence | Why it is here |
|---|---|---|---|
| `alembic` | 1.18.5 | MIT | Migrations |
| `annotated-doc` | 0.0.5 | MIT | FastAPI transitive |
| `annotated-types` | 0.8.0 | MIT | Pydantic transitive |
| `anyio` | 4.14.2 | MIT | Starlette transitive |
| `asyncpg` | 0.31.0 | Apache-2.0 | PostgreSQL driver |
| `certifi` | 2026.7.22 | **MPL-2.0** | CA bundle — see §4 |
| `click` | 8.4.2 | BSD-3-Clause | Uvicorn transitive |
| `fastapi` | 0.141.1 | MIT | Web framework |
| `greenlet` | 3.5.4 | MIT AND PSF-2.0 | SQLAlchemy async bridge |
| `h11` | 0.16.0 | MIT | HTTP/1.1 |
| `httpcore` | 1.0.9 | BSD-3-Clause | httpx transport |
| `httptools` | 0.8.0 | MIT | Uvicorn parser |
| `httpx` | 0.28.1 | BSD-3-Clause | Outbound HTTP |
| `idna` | 3.18 | BSD-3-Clause | httpx transitive |
| `jinja2` | 3.1.6 | BSD-3-Clause | Ops console templates |
| `mako` | 1.3.12 | MIT | Alembic templates |
| `markupsafe` | 3.0.3 | BSD-3-Clause | Jinja2 transitive |
| `pydantic` | 2.13.4 | MIT | Models and settings |
| `pydantic-core` | 2.46.4 | MIT | Pydantic core |
| `pydantic-settings` | 2.14.2 | MIT | Env configuration |
| `python-dotenv` | 1.2.2 | BSD-3-Clause | `.env` loading |
| `python-multipart` | 0.0.32 | Apache-2.0 | Console form parsing |
| `pyyaml` | 6.0.3 | MIT | Golden transcripts |
| `segno` | 1.6.6 | BSD-3-Clause | Attribution QR codes |
| `sentry-sdk` | 2.66.1 | MIT | Error reporting |
| `sqlalchemy` | 2.0.51 | MIT | ORM |
| `starlette` | 1.3.1 | BSD-3-Clause | ASGI framework |
| `structlog` | 26.1.0 | MIT OR Apache-2.0 | JSON logging |
| `tenacity` | 9.1.4 | Apache-2.0 | Retries |
| `typing-extensions` | 4.16.0 | PSF-2.0 | Typing back-ports |
| `typing-inspection` | 0.4.2 | MIT | Pydantic transitive |
| `urllib3` | 2.7.0 | MIT | Sentry transitive |
| `uvicorn` | 0.52.0 | BSD-3-Clause | ASGI server |
| `uvloop` | 0.22.1 | MIT | Event loop |
| `watchfiles` | 1.2.0 | MIT | Reload (dev only, ships with `uvicorn[standard]`) |
| `websockets` | 16.1.1 | BSD-3-Clause | Uvicorn extra |

`colorama` (BSD-3-Clause) appears in the resolved lock file as a Windows-only
conditional dependency of `click`. It is not installed on Linux and does not
ship in the image.

---

## Development — never leaves CI

47 packages: `pytest`, `ruff`, `mypy`, `respx`, `pre-commit`, `pip-audit` and
their transitives. Not distributed, not in the image, no obligation attaches.

| Licence | Packages |
|---|---|
| MIT | 27 |
| Apache-2.0 | 12 |
| BSD-2-Clause | 2 |
| BSD-3-Clause | 2 |
| PSF-2.0 | 2 |
| Apache-2.0 OR BSD-2-Clause | 1 |
| MPL-2.0 | 1 |

The MPL-2.0 entry is `pathspec`, a build-time path matcher. Same analysis as
`certifi` below, and even less exposed: it never reaches a deployment at all.

The full list is in `docs/evidence/licences.json` and regenerable at any time.

---

## The one that needs a sentence: `certifi` (MPL-2.0)

MPL-2.0 is weak copyleft. Its obligation is **file-level**: if you modify an
MPL-covered file and distribute the result, you must make that file's source
available under MPL. It does not reach files that merely link to or import it, and
it does not touch Mawjood's own source.

For Mawjood:

- `certifi` is installed from PyPI **unmodified**. No obligation is triggered.
- It is a CA certificate bundle plus a four-line accessor. Modifying it would be a
  strange thing to do.
- Mawjood is not distributed to third parties — it runs as a hosted service — so
  even a modified copy would not trigger the distribution obligation. MPL-2.0 has
  no network-use clause; that is AGPL, and nothing here is AGPL.

**Conclusion: no action required.** If someone ever vendors and patches
`certifi`, that patched file must be published under MPL-2.0. Note it in a code
comment if you do.

---

## Attribution obligations

MIT, BSD-2-Clause, BSD-3-Clause, ISC and Apache-2.0 all require the copyright
notice and licence text to be preserved **in distributions of the software**.

Mawjood distributes nothing today. The container image is built and run by the
operator, not handed to customers, and hosted use is not distribution under any
licence in this inventory.

**If that changes** — an on-premise deployment for an enterprise customer, an
image published to a public registry, a redistributed SDK — then before the first
delivery:

1. Produce a `THIRD_PARTY_NOTICES` file with each package's licence text.
   `pip-licenses --format=plain-vertical --with-license-file` is the usual tool.
2. Include it in the image and in any delivered archive.
3. Apache-2.0 additionally requires that a `NOTICE` file, where a package ships
   one, be reproduced. Three runtime packages are Apache-2.0 — check each.

That is a half-hour of work when it becomes necessary and zero before then, which
is why it has not been done.

---

## Patent grants

`asyncpg`, `python-multipart` and `tenacity` are Apache-2.0, which carries an
express patent licence from contributors and a termination clause if you sue
them over the software. Standard, and favourable — MIT and BSD say nothing about
patents at all.

---

## Policy for new dependencies

`CLAUDE.md` §8 already requires asking before adding a dependency. When you do,
check the licence at the same time:

| Licence | Action |
|---|---|
| MIT, BSD, ISC, Apache-2.0, PSF | Fine. Add it. |
| MPL-2.0, EPL | Fine if used unmodified. Note it here. |
| LGPL | Fine if dynamically linked and unmodified, but Python packaging makes "dynamically linked" ambiguous. Ask first. |
| GPL, AGPL | **No**, without a deliberate decision at the business level. AGPL in particular treats network use as distribution, which is exactly what Mawjood does. |
| SSPL, BUSL, "source available", Commons Clause | **No.** Not open source, and the terms usually prohibit offering the software as a service. |
| Unidentifiable | **No.** An unlicensed package grants no rights at all. |

Re-run `tools/licences.py` after adding anything and update the counts above.

---

## Vulnerability posture

Licences are one half of dependency hygiene; the other is `pip-audit`, run over
the full runtime and development closure. Results are in
`docs/evidence/dependency-audit.json` and summarised in `ACCEPTANCE.md`.

The two should be re-run together, on the same schedule — at minimum before every
release and monthly in between.
