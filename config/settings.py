"""Django settings for the data.gov.uk Explorer.

Environment config lives in .env, loaded here via python-dotenv. The env
var names (DATABASE_URL, APP_ENV, BASIC_AUTH_*, LLM_*) are generic and
double as the Django additions SECRET_KEY and DEBUG.
"""

import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def _db_config_from_url(url: str) -> dict:
    """Parse DATABASE_URL (postgresql://...) into Django's DATABASES dict.

    Empty host/port/user/password stay as "" so Django falls back to libpq
    defaults (local socket, current OS user).
    """
    p = urlparse(url)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": p.path[1:],
        "USER": p.username or "",
        "PASSWORD": p.password or "",
        "HOST": p.hostname or "",
        "PORT": p.port or "",
    }


SECRET_KEY = os.getenv("SECRET_KEY") or "dev-insecure-secret-key-change-me"

DEBUG = os.getenv("DEBUG", "false").lower() in ("1", "true", "yes")

# No host check by default; ALLOWED_HOSTS is opt-in via env.
ALLOWED_HOSTS = [h for h in os.getenv("ALLOWED_HOSTS", "*").split(",") if h]


INSTALLED_APPS = [
    # whitenoise.runserver_nostatic replaces runserver's stock staticfiles
    # handler with WhiteNoise (already in MIDDLEWARE), so in dev /static/
    # goes through the middleware chain too — that's what lets WhiteNoise's
    # Cache-Control settings below apply locally instead of runserver
    # short-circuiting static before any middleware runs. Must come before
    # django.contrib.staticfiles so this runserver command wins.
    "whitenoise.runserver_nostatic",
    # Static files — stock Django setup. Templates reference assets via
    # {{ static(...) }}; dev serves /static/ through WhiteNoise, prod needs
    # collectstatic into STATIC_ROOT.
    "django.contrib.staticfiles",
    "explorer",
]

MIDDLEWARE = [
    # Gates everything when APP_ENV=production and credentials are set.
    "explorer.middleware.BasicAuthMiddleware",
    # Renders templates/404.html for every 404 in both DEBUG modes (Django's
    # DEBUG technical 404 would otherwise replace it).
    "explorer.middleware.NotFoundMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves collectstatic output (STATIC_URL "/static/") in
    # production; in dev it just passes through to the staticfiles handler.
    # Missing static falls through to routing and renders 404.html.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        # Jinja2 is the template backend (macros + 5 custom filters),
        # listed first so render() resolves templates through it.
        "BACKEND": "explorer.jinja2.Jinja2",
        "DIRS": [BASE_DIR / "explorer" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {},
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# Database — Django owns the schema (migrations); the build pipeline
# populates it.
DATABASES = {
    "default": _db_config_from_url(
        os.getenv("DATABASE_URL", "postgresql://localhost:5432/datagovuk_explorer"),
    ),
}


# Internationalization

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True


# Static files — stock Django: STATIC_URL="/static/" is served by the
# staticfiles handler in dev (runserver) and by WhiteNoise in prod
# (collectstatic into STATIC_ROOT). Templates reference assets through
# {{ static('...') }} (the `static` Jinja2 global registered in
# explorer/jinja2.py). No MEDIA_URL override — we serve no media, and the
# Django default ("") normalises to "/", distinct from "/static/".
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "explorer" / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Caching — in dev WhiteNoise serves /static/ from STATICFILES_DIRS
# (finders) with max-age=0, so an edited CSS/JS file shows up on the next
# refresh instead of the browser's heuristic cache. Prod keeps WhiteNoise's
# default (60s): asset filenames have no content hashes, so a short max-age
# is the safe default there.
if DEBUG:
    WHITENOISE_USE_FINDERS = True
    WHITENOISE_MAX_AGE = 0
