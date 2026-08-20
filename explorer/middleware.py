"""HTTP basic auth + 404 template rendering.

BasicAuthMiddleware gates every request when APP_ENV=production and both
BASIC_AUTH_USER and BASIC_AUTH_PASS are set (incl. static + /health); the
WWW-Authenticate header is sent only when credentials are absent.

Dev-only note: whitenoise.runserver_nostatic (see config/settings.py) makes
WhiteNoise serve /static/ through the middleware chain locally too, so the
gate applies to static in both environments — unlike stock runserver, which
serves static before middleware when DEBUG=True.
"""

import base64
import binascii
import os

from django.http import Http404, HttpResponse

from . import views

BASIC_AUTH_USER = os.getenv("BASIC_AUTH_USER")
BASIC_AUTH_PASS = os.getenv("BASIC_AUTH_PASS")


class NotFoundMiddleware:
    """Render templates/404.html for every Http404, in both DEBUG modes.

    Django 6 shows its technical debug 404 page whenever DEBUG=True and a
    view raises Http404 — the custom handler404 only runs with DEBUG=False.
    This middleware restores the 404.html rendering in every environment:
    process_exception runs before Django's exception fallback, and every
    404 in this app originates in a view (page views raise Http404; the
    catch-all 404 view is reached for unrouted paths and missing static
    files), so nothing escapes it.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if isinstance(exception, Http404):
            return views.not_found(request, exception)
        return None


class BasicAuthMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.enabled = os.getenv("APP_ENV") == "production" and BASIC_AUTH_USER and BASIC_AUTH_PASS

    def __call__(self, request):
        if not self.enabled:
            return self.get_response(request)

        auth = request.headers.get("authorization")
        if not auth or not auth.startswith("Basic "):
            return HttpResponse(
                "Authentication required",
                status=401,
                headers={"WWW-Authenticate": 'Basic realm="data.gov.uk Explorer"'},
            )
        try:
            creds = base64.b64decode(auth[6:]).decode()
        except (binascii.Error, UnicodeDecodeError):
            return HttpResponse("Invalid credentials", status=401)
        user, _, pass_ = creds.partition(":")
        if user != BASIC_AUTH_USER or pass_ != BASIC_AUTH_PASS:
            return HttpResponse("Invalid credentials", status=401)
        return self.get_response(request)
