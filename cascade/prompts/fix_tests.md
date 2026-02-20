The tests are failing after an API/schema change was applied.

CHANGE THAT WAS APPLIED: {{CHANGE}}

REPOSITORY: {{REPO_NAME}}

TEST OUTPUT:
{{TEST_OUTPUT}}

Fix the failing tests. Follow these rules:
1. Read the test output carefully to understand what failed and why.
2. Fix ONLY the code needed to make the tests pass.
3. If a test expects old field names, update the test expectations.
4. If application code is wrong, fix it -- but keep changes minimal.
5. Do NOT delete tests or weaken assertions to make them pass.
6. After fixing, verify the code is syntactically valid.
