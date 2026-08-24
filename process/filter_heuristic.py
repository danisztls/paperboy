# SPDX-FileCopyrightText: 2026 Daniel de Souza
# SPDX-License-Identifier: AGPL-3.0-only

import re


def url_filtered(url: str, needles) -> bool:
    """Return True if `url` contains any of `needles` (the `skip.url_contains` config)."""
    if not needles:
        return False
    if isinstance(needles, str):
        needles = [needles]
    return any(n in url for n in needles)


def apply_regex(cfg, text: str) -> str:
    """Apply a flat `_TextTransform` op-set (`remove` / `extract` / `replace`+`with`) to text.

    `remove` is a raw regex (or list) `re.sub`'d out; `extract` keeps the match (group 1 if
    captured, else group 0); `replace`/`with` is a plain `re.sub`.
    """
    if not isinstance(cfg, dict):
        return text
    if remove := cfg.get("remove"):
        for pat in remove if isinstance(remove, list) else [remove]:
            text = re.sub(pat, "", text)
        text = re.sub(r"[ \t]+", " ", text).strip()
    if key := cfg.get("extract"):
        m = re.search(key, text)
        text = (m.group(1) if m.lastindex else m.group(0)) if m else text
    if "replace" in cfg:
        text = re.sub(cfg["replace"], cfg.get("with", ""), text)
    return text
