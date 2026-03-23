# delivery-gap-signals

Shared signal detection for the delivery-gap tool ecosystem.

Pure stateless functions for classifying commit messages and file paths. No I/O, no subprocess calls, no dependencies beyond the Python stdlib.

```
pip install delivery-gap-signals
```

## Used by

- [changeledger](https://github.com/brennhill/change-ledger) — cost per accepted change
- [CatchRate](https://github.com/brennhill/catchrate) — pipeline trustworthiness
- [Upfront](https://github.com/brennhill/upfront) — spec quality measurement

## Functions

```python
from delivery_gap_signals import (
    extract_ticket_ids,      # "Fixes PROJ-123, #456" → {"PROJ-123", "#456"}
    is_fix_message,          # "fix: null pointer" → True
    is_revert_message,       # 'Revert "add feature"' → True
    extract_fixes_sha,       # "Fixes: abc1234567" → "abc1234567"
    extract_revert_pr_numbers,  # "Reverts #42" → {42}
    extract_pr_number_from_subject,  # "Merge pull request #42 from ..." → 42
    is_source_file,          # "src/app.py" → True, "package-lock.json" → False
    compute_file_overlap,    # overlap ratio (% of candidate)
)
```

All functions are pure — no side effects, no I/O, no state.

## Why a separate package?

Three tools detect rework independently using similar patterns. Without a shared source of truth, the patterns drift and the tools disagree on which changes are reverts, fixes, or rework. This package ensures consistent detection across the ecosystem.
