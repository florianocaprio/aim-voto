from django.conf import settings
from django.core import signing
from django.urls import reverse

from .models import Delegation

SESSION_KEY = "proxy_access_v2"
TOKEN_PREFIX = "delega:"
TOKEN_SALT = "aim-voto.proxy-access"


def signer():
    return signing.Signer(salt=TOKEN_SALT)


def approved_delegation(binding):
    if (
        not isinstance(binding, list)
        or len(binding) != 4
        or any(type(part) is not int for part in binding)
    ):
        return None
    delegate, election, version, identifier = binding
    return (
        Delegation.objects.select_related("grantor__election")
        .filter(
            pk=identifier,
            delegate_id=delegate,
            grantor__election_id=election,
            grantor__election__allow_delegations=True,
            delegate__election_id=election,
            delegate__credential__version=version,
            state=Delegation.State.APPROVED,
        )
        .first()
    )


def link(delegation, credential):
    binding = [
        delegation.delegate_id,
        credential.voter.election_id,
        credential.version,
        delegation.pk,
    ]
    fragment = TOKEN_PREFIX + signer().sign_object(binding)
    return f"{settings.AIM_BASE_URL.rstrip('/')}{reverse('voting:access')}#{fragment}"


def decode(token):
    if len(token) > 512:
        return None
    try:
        return signer().unsign_object(token.removeprefix(TOKEN_PREFIX))
    except signing.BadSignature:
        return None
