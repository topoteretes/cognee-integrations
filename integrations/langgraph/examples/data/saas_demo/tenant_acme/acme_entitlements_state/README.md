# acme_entitlements_state (tenant_acme)

**Purpose:** Feature access ("entitlements") and seat allocation snapshots used by the product.

**Important detail for the demo**
- Subscription says Pro, invoice is Paid
- But entitlements snapshot still shows Trial/Read-only (exports + API disabled)
- last_synced_at is *yesterday* (stale / drift)

**Recommended NodeSets**
- `entitlements`
- `source:entitlements_service`

**Key concept**
- `EntitlementDrift`: Payment/subscription state and entitlement state disagree inside a short time window.
