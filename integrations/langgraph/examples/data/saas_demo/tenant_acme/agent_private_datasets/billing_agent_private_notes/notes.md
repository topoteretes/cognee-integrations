# Billing Agent Private Notes (Demo)

- Case: TICK-1001 (AcmeCorp)
- Observation: Invoice INV-94812 is paid (Stripe captured), but billing_account BA-ACME-001 was still `past_due` at 08:20Z.
- Hypothesis: dunning worker evaluated status from stale cache; late reconciliation at 09:05Z.
- Proposed fix: trigger billing account reconciliation + emit explicit `billing_account.status_changed` event for entitlements service.
