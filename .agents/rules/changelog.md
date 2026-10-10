---
paths:
  - "CHANGELOG.md"
---

# CHANGELOG entries

Write each entry for a user upgrading from the last release: what they will notice, and for a fix, what happened\
before.

- Compare with the last released section, not with earlier commits. A fix to something added since that release edits\
  the feature's Added entry; it is not a Fixed entry.
- Leave out how the change was built: internal modules, thread names, and review rounds.
- Put a breaking change first under Changed, with what to replace.
- Add entries under `## Unreleased`; the release commit renames that heading (CONTRIBUTING, "Versions and releases").
