---
description: Review code changes for bugs, regressions, and missing tests.
allowed-tools: memory_search
---
# code-review

1. Read the diff file-by-file and hunk-by-hunk. Do not run or apply changes.

2. Produce concrete issues first as a numbered list. For each issue include exactly: file path, line numbers or hunk context, concise title, severity (critical/high/medium/low), one-line impact, minimal actionable fix (diff-level pseudo-code), and specific tests to add (exact test names and expected assertions).

3. Authorization and data-access checks (follow exactly):
   - If any change replaces a direct return of a requested resource (e.g., "return requested_dataset" or similar) with a call to get_dataset(requested_dataset) (or equivalent), enforce access validation immediately before any return. Apply one of these two patterns and verify the diff shows it:
     - Caller-side validation pattern (preferred if get_dataset does not accept a requesting user):
       - Confirm the diff includes lines exactly equivalent to:
         dataset = get_dataset(requested_dataset)
         if dataset is None:
             return 404  # or raise NotFound
         if dataset.owner_id != user.id and not user_has_access(user, dataset):
             return 403  # or raise PermissionError
       - Confirm the code does not return dataset or expose dataset fields before these checks.
     - get_dataset-enforces-acl pattern (alternative):
       - Confirm get_dataset signature in the diff accepts the requesting user (e.g., get_dataset(id, requesting_user)) and that all call sites in the diff pass the requesting user.
       - Confirm get_dataset enforces ACLs internally and that callers handle get_dataset returning None or raising for missing/unauthorized resources (404/403) where appropriate.
   - If access enforcement was removed, weakened, or omitted by the change, mark the issue Critical. Provide exact diff-level pseudo-code to restore the caller-side checks above or to modify get_dataset signature and update call sites. Do not propose any fix that writes to persistent state; only propose code changes as pseudo-diffs.

4. Tests (follow exactly):
   - Confirm presence in the diff or associated test changes of unit/integration tests covering:
     - existing owner access (allowed)
     - non-owner access (denied)
     - missing dataset (404)
   - If any are missing, list explicit test names to add and the expected assertions, exactly as examples below:
     - test_get_dataset_returns_403_for_non_owner -> assert status == 403 or raises PermissionError
     - test_get_dataset_returns_404_for_missing_dataset -> assert status == 404 or raises NotFound
     - test_get_dataset_allows_owner -> assert returned dataset == expected
   - For each added test, include minimal setup lines (pseudo-code) and expected assertions.

5. Logging & auditing (follow exactly):
   - Check for an audit/log entry at the access decision point that includes user id, dataset id (or requested id if dataset is None), and result ("allowed"/"denied").
   - If missing, propose adding a single audit line at the decision point. Example minimal placement and fields (use verbatim or equivalent):
     audit.log("dataset.access", user_id=user.id, dataset_id=(dataset.id if dataset else requested_dataset), result="allowed"/"denied")
   - Do not propose logging that would mutate state beyond minimal audit calls.

6. Regression & call-site checks:
   - Search the diff and nearby files for all other call sites of get_dataset or any direct returns of requested_dataset. List all impacted call sites with file and context.
   - For each inconsistent call site, propose consistent fixes (provide diff-level pseudo-code similar to step 3) and corresponding tests to add or update.

7. Output format required from this review (follow exactly):
   - Produce a numbered list of issues as required in step 2.
   - For each issue include a one-line recommended patch (pseudo-diff) and specific tests to add or update.
   - For Critical issues include reproduction steps and a short-term mitigation before merge (e.g., reintroduce owner check at caller).
   - Do not modify repository state; only report findings and suggested patches.

8. Severity guidance (use these labels):
   - Critical: removed or weakened authorization checks that may expose data.
   - High: missing tests for critical access paths.
   - Medium: missing logging/audit or non-security regressions.
   - Low: style/clarity suggestions.

9. Failures found in CI or prior runs:
   - Summarize as issues following step 2. For Critical failures include reproduction steps and a short-term mitigation.

10. Additional strict rules:
   - Never propose fixes that require executing writes or mutating production data as part of the fix.
   - When proposing signature changes (e.g., get_dataset(id, requesting_user)), include pseudo-diff changes for all affected call sites in the diff.
   - When proposing a caller-side enforcement patch, include the exact minimal lines to add in the pseudo-diff and the single audit line placement.
   - Always keep recommendations minimal and actionable (diff-level pseudo-code and test names/assertions only).

11. Final delivery rule:
   - Return only the required numbered issues report (no extra narrative).
