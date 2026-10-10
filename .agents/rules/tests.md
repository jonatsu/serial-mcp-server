---
paths:
  - "tests/**"
---

# Tests

- Watch a regression test go red: set its fix aside (`git stash push -- <file>`), run the test, see it fail, then\
  restore the fix and see it pass.
- Assert on what the code did, not on what a mock was told to return. In `tests/conftest.py`, `mock_serial` gives `dtr`\
  and `rts` a `PropertyMock` that always returns `True`, so reading `conn.ser.dtr` proves nothing; assert the setter\
  calls with `vars(type(conn.ser))["dtr"].call_args_list`.
