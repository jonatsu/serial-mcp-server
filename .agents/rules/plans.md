---
paths:
  - "docs/plans/**"
  - "docs/roadmap.md"
---

# Plans and the roadmap

- Close a finished plan in one commit: move its open follow-ups into `docs/roadmap.md`, set its status line, and\
  `git mv` the plan and its design into `docs/plans/archive/`, fixing their relative links and every link to them.
- Place a new roadmap item by the priority rule at the top of `docs/roadmap.md`, and remove an item in the commit that\
  lands its fix, with the CHANGELOG entry.
