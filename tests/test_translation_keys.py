"""CI guard against translation-key drift.

`flash(_("auth.2fa.enrolled"), ...)` and `{{ _("admin.users.created") }}`
both pass string keys to gettext. A typo silently renders the raw key
to the operator. This test walks the codebase, extracts every key
passed to `_(...)` or `gettext(...)`, and asserts each one exists in
*both* translation catalogs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent / "app"
CATALOGS = ROOT / "translations"

# Match _("...") or gettext("...") with single or double quotes. We
# only care about literal-string keys; dynamic keys (rare in this
# codebase) would need a different strategy.
KEY_RE = re.compile(r"""(?:\b_|\bgettext)\(\s*['"]([a-z0-9_.]+)['"]""")


def _collect_keys() -> set[str]:
    keys: set[str] = set()
    for path in ROOT.rglob("*.py"):
        keys.update(KEY_RE.findall(path.read_text(encoding="utf-8")))
    for path in ROOT.rglob("*.html"):
        keys.update(KEY_RE.findall(path.read_text(encoding="utf-8")))
    # Drop prefix-only matches like `_("tickets.severity." + sev)` —
    # the suffix is dynamic, so the regex captures only the prefix.
    return {k for k in keys if not k.endswith(".")}


def _load_catalog(lang: str) -> dict[str, str]:
    return json.loads((CATALOGS / f"{lang}.json").read_text(encoding="utf-8"))


def test_every_referenced_key_exists_in_every_catalog():
    keys = _collect_keys()
    assert keys, "No translation keys found — KEY_RE may be broken"
    missing: dict[str, list[str]] = {}
    for lang in ("pl", "en"):
        catalog = _load_catalog(lang)
        absent = sorted(k for k in keys if k not in catalog)
        if absent:
            missing[lang] = absent
    assert not missing, (
        "Translation keys referenced in code/templates but missing from "
        f"the JSON catalogs:\n{json.dumps(missing, indent=2, ensure_ascii=False)}"
    )
