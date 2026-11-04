# acme_billing_finance (tenant_acme)

**Purpose:** Invoices, payments, billing account state, and dunning/grace-period logic.

**Narrative for the incident**
- Invoice `INV-94812` for seat true-up was paid (captured) at 08:17.
- However, billing account `BA-ACME-001` remained `past_due` due to a delayed webhook reconciliation of an older invoice `INV-94700`.
- Entitlements logic gated on `billing_account_status`, triggering an incorrect downgrade despite payment.

**Recommended NodeSets**
- `finance`
- `stripe`
- `invoices`
- `payments`
- `dunning`
