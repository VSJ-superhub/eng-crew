---
name: repair-verification-failure
description: Fix a change that failed the verification gate, starting from a cold context. Used by VerifierAgent when no CLI session is available to resume.
---
The change below was implemented, but verification failed. Fix it.

=== ORIGINAL TASK ===
{task}

=== VERIFICATION FAILURES ===
{failures}

Fix the cause of these failures. Read the relevant code first — the failure may be in the
implementation or in a test that the change made stale, so decide which is actually wrong
rather than forcing either to match the other. Do not delete, skip, or weaken a test to make
it pass. Re-run the failing command yourself to confirm the fix before you finish.
