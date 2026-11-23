from django.conf import settings
from django.http import HttpResponse
from django.utils.cache import add_never_cache_headers


class PrivateResponses:
    """Reject oversized requests and apply privacy headers to dynamic responses."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            content_length = int(request.META.get("CONTENT_LENGTH") or "0")
        except ValueError:
            content_length = -1
        if content_length < 0:
            response = HttpResponse("Lunghezza della richiesta non valida.", status=400)
        elif content_length > settings.MAX_REQUEST_BYTES:
            response = HttpResponse("La richiesta supera le dimensioni massime consentite.", status=413)
        else:
            response = self.get_response(request)
        add_never_cache_headers(response)
        response["Referrer-Policy"] = "same-origin"
        response["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        response["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response
