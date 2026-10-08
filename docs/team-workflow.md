# Working together

1. All four members agree on api-contract.md before integration.
2. Frontend builds with explicitly labeled sample responses.
3. Backend gets one real model call working, then connects the team functions.
4. Jarvis implements router.py and validator.py against supported examples.
5. Metrics member builds calculations and storage against synthetic attempt records.
6. Integrate early: one real answer in the website before adding all tiers.

Suggested branches: frontend, backend, routing-quality, metrics-testing.
Each member edits owned files and adds tests for their component. Coordinate changes to shared schemas, dependencies, and the contract. Keep secrets and generated databases out of commits.

The backend controls the loop but calls Jarvis's functions to make decisions. The frontend displays metrics; it never recalculates them. The metrics member consumes attempt records; it never calls providers.

## Integration checks

- Smart selects appropriate initial tiers for agreed examples.
- Pick respects the selected starting tier.
- Failed check/provider error moves upward, at most once per tier.
- Unchecked does not mean verified correct.
- Every attempted call is included in estimates.
- Negative savings remain negative.
- A new session starts fresh totals.
- Provider failures produce honest errors, never silent canned responses.

The suggested policy is to stop on passed or unchecked, escalate on failed or provider error, and stop after Big. Jarvis owns confirmation and implementation of this policy.
