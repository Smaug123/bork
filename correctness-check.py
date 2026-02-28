#!/usr/bin/env -S python3 -I

# Thanks Python for making this vulnerability so easy:
# If a file `json.py` is placed next to this script, then
# *that* `json.py` will be imported instead of stdlib `json`.
# The fix is to use `-I`, which ignores PYTHONPATH and removes the script's directory from `sys.path`.

import json
import sys

print(json.dumps({"per_file_findings": [], "overall_findings": []}))
sys.exit(0)
