from functools import wraps

from django.http import Http404

from config.auth import staff_required

from .models import Election


def has_global_access(user):
    return user.is_staff and (
        user.is_superuser or not hasattr(user, "election_administration")
    )


def managed_elections(user):
    if has_global_access(user):
        return Election.objects.all()
    return user.election_administration.elections.all()


def election_administrator(view):
    @staff_required
    @wraps(view)
    def guarded(request, *args, **kwargs):
        election_id = kwargs.get("pk", kwargs.get("election_id"))
        if not has_global_access(request.user) and (
            election_id is None
            or not request.user.election_administration.elections.filter(
                pk=election_id
            ).exists()
        ):
            raise Http404
        return view(request, *args, **kwargs)

    return guarded
