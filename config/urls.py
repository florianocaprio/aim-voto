from django.db import connection
from django.db.utils import DatabaseError
from django.http import HttpResponse
from django.urls import include, path
from django.views.decorators.http import require_safe

from .auth import StaffLogin, StaffLogout, staff_otp


@require_safe
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        return HttpResponse("database unavailable", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path("healthz/", health, name="health"),
    path("account/accesso/", StaffLogin.as_view(), name="staff_login"),
    path("account/uscita/", StaffLogout.as_view(), name="staff_logout"),
    path("account/verifica/", staff_otp, name="staff_otp"),
    path("gestione/soci/", include("members.urls")),
    path("gestione/invii/", include("mailings.urls")),
    path("", include("voting.urls")),
]
