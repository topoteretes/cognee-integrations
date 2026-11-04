# acme_subscriptions_core (tenant_acme)

**Purpose:** Canonical subscription state as written by the billing/subscription service.

**Key IDs used across datasets**
- tenant_id: `TEN-ACME`
- customer_account_id: `CA-ACME-001`
- billing_account_id: `BA-ACME-001`
- workspace_id: `W-332`
- subscription_id: `SUB-483`
- invoice_id: `INV-94812` (seat true-up invoice)

**Recommended NodeSets when ingesting**
- `subscriptions`
- `source:billing_db`

**Narrative:**
- Subscription is Pro Annual with 50 seats purchased.
- Customer reports being downgraded to Trial/Read-only "yesterday" despite invoice `INV-94812` being paid.
