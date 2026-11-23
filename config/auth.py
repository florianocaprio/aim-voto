from functools import wraps
from urllib.parse import urlencode

import django_otp
from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView, LogoutView, redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods


def staff_required(view):
    @wraps(view)
    def guarded(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), "staff_login")
        if not request.user.is_staff:
            raise PermissionDenied
        if settings.REQUIRE_ADMIN_MFA and not request.user.is_verified():
            return redirect_to_login(request.get_full_path(), "staff_otp")
        return view(request, *args, **kwargs)

    return guarded


def require_superadmin(user):
    if not (user.is_active and user.is_staff and user.is_superuser):
        raise PermissionDenied


def superadmin_required(view):
    @staff_required
    @wraps(view)
    def guarded(request, *args, **kwargs):
        require_superadmin(request.user)
        return view(request, *args, **kwargs)

    return guarded


class StaffAuthenticationForm(AuthenticationForm):
    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.is_staff:
            raise self.get_invalid_login_error()


class StaffLogin(LoginView):
    authentication_form = StaffAuthenticationForm
    template_name = "config/login.html"

    def post(self, request, *args, **kwargs):
        from voting.services import admit_request

        if not admit_request("accesso-admin", request.META.get("REMOTE_ADDR", ""), 40, 600):
            return HttpResponse("Troppi tentativi. Riprova fra qualche minuto.", status=429)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        if settings.REQUIRE_ADMIN_MFA:
            return redirect(reverse("staff_otp") + "?" + urlencode({"next": self.get_success_url()}))
        return response


class StaffLogout(LogoutView):
    http_method_names = ("post", "options")


class StaffOTPForm(forms.Form):
    token = forms.RegexField(
        label="Codice di verifica",
        regex=r"^[0-9]{6}$",
        widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "one-time-code"}),
    )


def management_target(request):
    target = request.POST.get("next") or request.GET.get("next")
    if target and target.startswith("/gestione/") and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return settings.LOGIN_REDIRECT_URL


@require_http_methods(["GET", "POST"])
def staff_otp(request):
    from voting.services import admit_request

    if not request.user.is_authenticated:
        return redirect("staff_login")
    if not request.user.is_staff:
        raise PermissionDenied
    target = management_target(request)
    if not settings.REQUIRE_ADMIN_MFA or request.user.is_verified():
        return redirect(target)
    devices = list(django_otp.devices_for_user(request.user, confirmed=True))
    form = StaffOTPForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and devices:
        if not admit_request("totp-admin", request.user.pk, 8, 600):
            return HttpResponse("Troppi tentativi. Riprova fra qualche minuto.", status=429)
        if form.is_valid():
            for device in devices:
                verified = django_otp.verify_token(
                    request.user, device.persistent_id, form.cleaned_data["token"]
                )
                if verified:
                    request.session.cycle_key()
                    django_otp.login(request, verified)
                    return redirect(target)
            form.add_error("token", "Codice non valido o già utilizzato.")
    return render(request, "config/otp.html", {"form": form, "devices": devices, "next": target})
