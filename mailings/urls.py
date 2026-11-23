from django.urls import path

from . import views

app_name = "mailings"
urlpatterns = [
    path("<int:election_id>/inviti/", views.invite, name="invite"),
    path("<int:election_id>/sollecita/", views.remind, name="remind"),
    path("<int:election_id>/reinvia/<int:voter_id>/", views.resend, name="resend"),
    path("<int:election_id>/", views.history, name="history"),
    path("<int:election_id>/intervallo/", views.interval, name="interval"),
    path(
        "<int:election_id>/<uuid:campaign_id>/", views.campaign_detail, name="campaign"
    ),
    path(
        "<int:election_id>/<uuid:campaign_id>/conferma/", views.confirm, name="confirm"
    ),
    path("<int:election_id>/<uuid:campaign_id>/annulla/", views.cancel, name="cancel"),
]
