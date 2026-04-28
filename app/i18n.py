"""Lightweight i18n module using Babel for formatting + JSON message catalogs.

Why custom (not Flask-Babel)?
    Flask-Babel is unavailable in this environment. We replicate the surface
    we need: gettext, lazy_gettext, language detection from cookie/header/user
    preference, and dynamic JSONB-style content lookup.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from flask import Flask, current_app, g, request

TRANSLATIONS_DIR = Path(__file__).parent / "translations"


@lru_cache(maxsize=8)
def _load_catalog(lang: str) -> dict[str, str]:
    path = TRANSLATIONS_DIR / f"{lang}.json"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def gettext(key: str, **kwargs: Any) -> str:
    lang = getattr(g, "lang", None) or current_app.config["DEFAULT_LANGUAGE"]
    catalog = _load_catalog(lang)
    text = catalog.get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text


_ = gettext  # alias


def i18n_field(jsonb_field: dict | None, lang: str | None = None) -> str:
    """Resolve a multi-language JSON field {pl, en} for current language."""
    if not jsonb_field:
        return ""
    lang = lang or getattr(g, "lang", None) or "en"
    return (
        jsonb_field.get(lang)
        or jsonb_field.get("en")
        or next(iter(jsonb_field.values()), "")
    )


def detect_language() -> str:
    supported = current_app.config["SUPPORTED_LANGUAGES"]
    default = current_app.config["DEFAULT_LANGUAGE"]

    # 1. authenticated user preference
    from flask_login import current_user

    if current_user.is_authenticated and getattr(current_user, "language", None):
        if current_user.language in supported:
            return current_user.language

    # 2. cookie
    cookie_lang = request.cookies.get("lang")
    if cookie_lang and cookie_lang in supported:
        return cookie_lang

    # 3. Accept-Language header
    accept = request.accept_languages.best_match(list(supported))
    if accept:
        return accept

    return default


def init_i18n(app: Flask) -> None:
    """Register before_request hook + Jinja globals."""
    os.makedirs(TRANSLATIONS_DIR, exist_ok=True)

    @app.before_request
    def _set_lang():
        g.lang = detect_language()

    app.jinja_env.globals["_"] = gettext
    app.jinja_env.globals["gettext"] = gettext
    app.jinja_env.filters["i18n"] = i18n_field
