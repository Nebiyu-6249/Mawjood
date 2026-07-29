# tools

Operator and developer scripts. Each is a standalone CLI, not part of the
`mawjood` package.

| Script | Purpose | Phase |
|---|---|---|
| `chat_sim.py` | Local terminal chat against the conversation engine. **Requires no WhatsApp credentials and no live aggregator access** — fakes only. The primary development loop. | 2 |
| `seed_routing.py` | Seed the category → aggregator priority config. | 3 |
| `make_qr.py` | Generate attribution QR codes carrying a source identifier. | 3 |
| `erase_consumer.py` | PDPL deletion: purge a consumer across every table including logs. | 4 |

When the first script lands, add `tools` back to `files` in `[tool.mypy]`.
mypy treats an empty directory as a fatal error, which is why it is absent today.
