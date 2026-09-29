---
name: test-first
description: Test-first working order, injected into a builder's prompt when the task carries acceptance criteria. Used by SingleAgentEngineer.
---
=== ACCEPTANCE CRITERIA ===
{criteria}

Work test-first. This ordering takes precedence over the general approach below:

1. Read enough of the code to know where each criterion belongs.
2. Write tests that encode the criteria above — one test per criterion, in the
   project's existing test suite and style. A criterion you cannot express as a
   test is a criterion to say so about in your summary, not one to skip quietly.
3. Run those tests and confirm they FAIL, for the reason you expect. A new test
   that passes before you have implemented anything is testing the wrong thing —
   rewrite it until it fails. Do not move on from this step.
4. Now implement, until those tests pass and the rest of the suite stays green.
5. Do not weaken or delete a test to make it pass. The tests are the deliverable
   as much as the code is, and a later gate replays them against the pre-change
   code to check they discriminate.

In your final summary, state which test covers which criterion, and say
explicitly that you saw the new tests fail before the implementation.
