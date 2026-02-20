You are applying an API/schema change to a {{LANGUAGE}} codebase.

CHANGE: {{CHANGE}}

REPOSITORY: {{REPO_NAME}}

Instructions:
1. Read ALL files in this repository to understand the codebase structure.
2. Find every reference to the old fields, types, or API shape.
3. Update data models, type definitions, and schemas.
4. Update all code that reads, writes, transforms, or displays the affected data.
5. Update all tests to use the new field names and expected values.
6. Update string literals, format strings, and template variables.
7. Update documentation and comments.

RULES:
- Make MINIMAL changes -- only modify what is directly affected by the change.
- Do NOT refactor, restructure, or "improve" unrelated code.
- Ensure the code remains syntactically valid after changes.
- If unsure about a change, err on the side of making it (false negatives are worse than false positives).
- After making all changes, run a quick sanity check to verify nothing is obviously broken.
