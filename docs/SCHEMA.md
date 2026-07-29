# Schema

Eleven tables. Every one except `tenants` carries `tenant_id` — the root of the
scoping hierarchy is the only thing not scoped by it.

```mermaid
erDiagram
    TENANTS ||--o{ LEADS : "scopes"
    TENANTS ||--o{ ROUTING_CONFIG : "scopes"
    LEADS ||--o{ CONVERSATIONS : "has"
    LEADS ||--o{ CONSENTS : "grants"
    LEADS ||--o{ ATTRIBUTION : "came from"
    CONVERSATIONS ||--o{ MESSAGES : "contains"
    CONVERSATIONS ||--o{ BOOKINGS : "produces"
    CONVERSATIONS ||--o{ HANDOFF_QUEUE : "escalates to"
    CONVERSATIONS ||--o{ AUDIT_LOG : "records"
    BOOKINGS ||--o{ FEEDBACK : "rated by"
    MESSAGES ||--o{ CONSENTS : "evidences"

    TENANTS {
        uuid id PK
        string slug UK
        string name
        string default_locale
        string timezone
        bool is_active
    }

    LEADS {
        uuid id PK
        uuid tenant_id FK
        string wa_id "UK with tenant_id"
        string phone_e164 "PII"
        string display_name "PII"
        string locale
        ts first_seen_at
        ts last_seen_at
    }

    CONVERSATIONS {
        uuid id PK
        uuid tenant_id FK
        uuid lead_id FK
        enum channel "whatsapp|console"
        enum status "active|awaiting_handoff|handed_off|closed"
        bool bot_muted "human owns the thread"
        ts last_activity_at
    }

    MESSAGES {
        uuid id PK
        bigint seq UK "total order"
        uuid tenant_id FK
        uuid conversation_id FK
        uuid turn_id "ties to audit_log"
        enum direction "inbound|outbound"
        enum status
        text body
        string phrasebank_key "NOT NULL when outbound"
        string provider_message_id "partial UK: idempotency"
    }

    CONSENTS {
        uuid id PK
        uuid tenant_id FK
        uuid lead_id FK
        enum event "notice_shown|granted|withdrawn"
        string policy_version
        text wording "exact text shown"
        string locale
        uuid evidence_message_id FK
        ts occurred_at
    }

    BOOKINGS {
        uuid id PK
        uuid tenant_id FK
        uuid conversation_id FK
        enum category
        string platform_slug
        string idempotency_key "UK with tenant_id"
        enum status "incl. uncertain"
        ts slot_start
        numeric price_amount
        enum payment_status "no payment flow in v1"
    }

    ROUTING_CONFIG {
        uuid id PK
        uuid tenant_id FK
        enum category
        string platform_slug "no FK by design"
        int position "UK with tenant+category"
        bool is_enabled
        jsonb config
    }

    HANDOFF_QUEUE {
        uuid id PK
        uuid tenant_id FK
        uuid conversation_id FK
        enum reason "cascade_exhausted|unsupported_action|..."
        enum status "open|claimed|resolved"
        jsonb context
    }

    ATTRIBUTION {
        uuid id PK
        uuid tenant_id FK
        uuid lead_id FK
        string source_code "SRC12"
        enum medium "wa_link|qr|direct"
        bool is_first_touch
    }

    FEEDBACK {
        uuid id PK
        uuid tenant_id FK
        uuid booking_id FK
        int rating "CHECK 1..5"
        text comment
    }

    AUDIT_LOG {
        uuid id PK
        bigint seq UK "total order"
        uuid tenant_id FK
        uuid turn_id "one inbound message"
        uuid decision_id "one cascade run"
        string correlation_id "ties to log lines"
        string event_type
        string platform_slug
        int attempt_number
        string outcome
        int latency_ms
        string advance_reason "why it moved on"
        bool consumer_visible
        text consumer_text "what they saw"
        jsonb inputs
        jsonb details
    }
```

## Notes on choices that are not obvious

**`seq` on `messages` and `audit_log`.** `created_at` cannot order rows written in
one transaction: PostgreSQL's `now()` is transaction start time, so every row in a
turn shares a timestamp and the tiebreak falls to a random UUID. Without `seq`, a
transcript reads back in arbitrary order — the inbound message can sort between
the two replies to it.

**`audit_log` is append-only, enforced by a trigger.** `UPDATE` is refused
outright. `DELETE` is refused unless the session sets `mawjood.allow_purge`, which
is how the PDPL erasure path still works while casual deletion does not.

**`audit_log.event_type` and `.outcome` are unconstrained text.** Every other
enum column is a VARCHAR with a CHECK. This one must not be: an audit write that
fails because of an unrecognised value destroys the record explaining what
happened.

**`messages.phrasebank_key` is CHECKed NOT NULL for outbound rows.** Consumer copy
that cannot name its phrasebank entry has bypassed the no-empty-shelves blocklist
scan.

**`routing_config.platform_slug` has no foreign key.** The adapter registry
(Phase 2) is the authority on which slugs exist; this table only orders them.

**`consents` is an event stream, not a flag.** Current state is derived. What a
consumer was shown, and when, survives intact.

## Deferred to Phase 2

`platforms`, `merchants` and `merchant_credentials` (decision 1 — many merchants
per platform). Nothing consumes them until the adapter layer exists, and adding
them is a clean additive migration.
