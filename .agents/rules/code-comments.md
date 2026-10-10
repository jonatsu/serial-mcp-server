---
paths:
  - "serial_mcp_server/**/*.py"
---

# Comments on limits and hidden behavior

- Give each hardcoded limit a comment that says what happens when it is reached (dropped, clamped, rejected, or\
  blocked) and why it has that value.
- Document what a caller cannot see from the signature: where cancellation takes effect, a unit that differs from the\
  name (`trace_max_bytes` counts characters), and a copy that is only best effort.
- State a default where it is defined, and name the constant elsewhere, so a changed default leaves no stale number.
- Check each claim against the code before writing it; a comment says what the code does now.
