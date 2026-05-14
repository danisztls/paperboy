import re

_PHRASE_URL_RE = re.compile(r"[^.!?\n]*?https?://\S+")


def _remove_phrases_with_urls(text: str) -> str:
    text = _PHRASE_URL_RE.sub("", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def url_filtered(url: str, cfg) -> bool:
    """Return True if the URL should be excluded by the url filter config."""
    if not cfg:
        return False
    if isinstance(cfg, list):
        return any(url_filtered(url, item) for item in cfg)
    needles = cfg.get("skip_containing")
    if not needles:
        return False
    if isinstance(needles, str):
        needles = [needles]
    return any(n in url for n in needles)


def apply_regex(cfg, text: str) -> str:
    if not isinstance(cfg, (list, dict)):
        return text
    if isinstance(cfg, list):
        for item in cfg:
            text = apply_regex(item, text)
        return text
    if isinstance(cfg, dict):
        if cfg.get("clear"):
            return ""
        if cfg.get("remove_phrases_with_urls"):
            text = _remove_phrases_with_urls(text)
        if needle := cfg.get("remove_phrases_containing"):
            needles = needle if isinstance(needle, list) else [needle]
            for n in needles:
                text = re.sub(r"[^.!?\n]*?" + re.escape(n) + r"[^.!?\n]*", "", text)
            text = re.sub(r"[ \t]+", " ", text).strip()
        if key := cfg.get("extract"):
            m = re.search(key, text)
            text = (m.group(1) if m.lastindex else m.group(0)) if m else text
        if "replace" in cfg:
            text = re.sub(cfg["replace"], cfg.get("with", ""), text)
        return text
