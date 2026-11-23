import os
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise ImproperlyConfigured(f"Impostare {name} prima di avviare AIM Voto.")
    return value


def flag(name, default="0"):
    value = os.getenv(name, default)
    if value not in ("0", "1"):
        raise ImproperlyConfigured(f"{name} accetta 0 oppure 1.")
    return value == "1"


def positive_integer(name, default):
    try:
        value = int(os.getenv(name, default))
    except ValueError:
        value = 0
    if value < 1:
        raise ImproperlyConfigured(f"{name} richiede un intero maggiore di zero.")
    return value


PROFILE = os.getenv("AIM_PROFILE", "development")
if PROFILE not in {"development", "production"}:
    raise ImproperlyConfigured("AIM_PROFILE accetta development oppure production.")
PRODUCTION = PROFILE == "production"
DEBUG = flag("AIM_DEBUG")
SECRET_KEY = required("DJANGO_SECRET_KEY")
if len(SECRET_KEY) < 50:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY deve contenere almeno 50 caratteri casuali.")

VOTING_LINK_KEYS = required("VOTING_LINK_KEYS").split(",")
try:
    for key in VOTING_LINK_KEYS:
        Fernet(key)
except (TypeError, ValueError) as exc:
    raise ImproperlyConfigured("VOTING_LINK_KEYS richiede una lista di chiavi Fernet valide.") from exc

AIM_BASE_URL = os.getenv("AIM_BASE_URL", "http://localhost:8000").rstrip("/")
parsed_url = urlsplit(AIM_BASE_URL)
if (
    parsed_url.scheme not in ("http", "https")
    or not parsed_url.hostname
    or parsed_url.username is not None
    or parsed_url.password is not None
    or parsed_url.path
    or parsed_url.query
    or parsed_url.fragment
):
    raise ImproperlyConfigured("AIM_BASE_URL richiede un'origine senza percorso, query o credenziali.")
ALLOWED_HOSTS = [parsed_url.hostname]
if not PRODUCTION:
    ALLOWED_HOSTS = list(dict.fromkeys([*ALLOWED_HOSTS, "localhost", "127.0.0.1", "[::1]"]))
REQUIRE_ADMIN_MFA = flag("REQUIRE_ADMIN_MFA", "1" if PRODUCTION else "0")
if PRODUCTION and (DEBUG or not REQUIRE_ADMIN_MFA or parsed_url.scheme != "https"):
    raise ImproperlyConfigured("Il profilo production richiede HTTPS, MFA e AIM_DEBUG=0.")

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "config.apps.ConfigApp",
    "voting",
    "members",
    "mailings",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "config.middleware.PrivateResponses",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", "aim_voto"),
        "USER": os.getenv("POSTGRES_USER", "aim_voto"),
        "PASSWORD": required("POSTGRES_PASSWORD"),
        "HOST": os.getenv("POSTGRES_HOST", "127.0.0.1"),
        "PORT": positive_integer("POSTGRES_PORT", "5432"),
        "OPTIONS": {"connect_timeout": 5},
        "TEST": {"NAME": os.getenv("AIM_TEST_DB")},
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 14},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LOGIN_URL = "staff_login"
LOGIN_REDIRECT_URL = "/gestione/"
LOGOUT_REDIRECT_URL = "staff_login"
OTP_TOTP_ISSUER = "AIM Voto"

LANGUAGE_CODE = "it"
TIME_ZONE = "Europe/Rome"
USE_TZ = True
USE_I18N = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = PRODUCTION
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 60 * 60 * 8
CSRF_COOKIE_SECURE = PRODUCTION
CSRF_COOKIE_SAMESITE = "Lax"
# A no-referrer policy can turn the Origin of a browser form POST into null.
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_SSL_REDIRECT = PRODUCTION
SECURE_HSTS_SECONDS = 31_536_000 if PRODUCTION else 0
X_FRAME_OPTIONS = "DENY"
if PRODUCTION and flag("AIM_TRUST_HTTPS_PROXY"):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

MAX_REQUEST_BYTES = 6 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FILES = 5
DATA_UPLOAD_MAX_NUMBER_FIELDS = 15_000
FILE_UPLOAD_MAX_MEMORY_SIZE = 512 * 1024

EMAIL_HOST = os.getenv("EMAIL_HOST", "127.0.0.1")
if EMAIL_HOST not in {"localhost", "127.0.0.1", "::1", "mailpit"}:
    raise ImproperlyConfigured("Questa versione ammette solo SMTP locale o il servizio Mailpit.")
EMAIL_PORT = positive_integer("EMAIL_PORT", "1025")
EMAIL_TIMEOUT = positive_integer("EMAIL_TIMEOUT", "10")
EMAIL_HOST_USER = ""
EMAIL_HOST_PASSWORD = ""
EMAIL_USE_TLS = False
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "aim-voto@example.invalid")
SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "assistenza@example.invalid")
MAIL_SEND_PER_MINUTE = positive_integer("MAIL_SEND_PER_MINUTE", "30")
MAIL_MAX_ATTEMPTS = positive_integer("MAIL_MAX_ATTEMPTS", "3")

# Django's default request and security log records can contain the request path and
# their exception context can contain voter information. Only explicit, sanitized audit
# events belong in the application register. Gunicorn access logging stays disabled.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"discard": {"class": "logging.NullHandler"}},
    "loggers": {
        name: {"handlers": ["discard"], "propagate": False}
        for name in ("django.server", "django.request", "django.security")
    },
}
