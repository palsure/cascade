You are analyzing a {{LANGUAGE}} codebase to find all files affected by an API change.

CHANGE: {{CHANGE}}

REPOSITORY: {{REPO_NAME}} ({{LANGUAGE}})

Analyze every file in this repository and list ALL files that reference the affected fields, types, or API endpoints. Be thorough -- check:

1. **Data models / types** -- classes, interfaces, dataclasses, schemas
2. **API clients** -- fetch calls, HTTP requests, SDK methods
3. **Business logic** -- functions that read, transform, or display the affected data
4. **Tests** -- any test that creates, asserts, or mocks the affected fields
5. **Configuration** -- any config files that reference field names
6. **Documentation** -- READMEs, comments, docstrings mentioning the fields

For each affected file, state:
- The file path
- What specifically needs to change
- The severity (critical = will break, moderate = should update, low = cosmetic)
