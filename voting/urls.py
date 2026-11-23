from django.urls import path

from . import assembly_views, views

app_name = "voting"
urlpatterns = [
    path("", views.home, name="home"),
    path("accedi/", views.access, name="access"),
    path("scheda/", views.ballot, name="ballot"),
    path("scheda/presenza-e-deleghe/", assembly_views.voter_assembly, name="assembly"),
    path("scheda/deleghe/cerca/", assembly_views.find_delegate, name="find_delegate"),
    path(
        "scheda/deleghe/scelta/<int:delegate_pk>/",
        assembly_views.confirm_direct_delegation,
        name="confirm_direct_delegation",
    ),
    path(
        "scheda/deleghe/presenta/",
        assembly_views.find_grantor,
        name="received_delegation",
    ),
    path(
        "scheda/deleghe/presenta/<int:grantor_pk>/",
        assembly_views.submit_received_delegation,
        name="received_delegation_file",
    ),
    path(
        "scheda/deleghe/<int:delegation_pk>/", views.proxy_ballot, name="proxy_ballot"
    ),
    path(
        "scheda/deleghe/<int:delegation_pk>/revoca/",
        assembly_views.voter_revoke,
        name="voter_revoke",
    ),
    path("scheda/conferma/", views.submit, name="submit"),
    path("scheda/documenti/<int:pk>/", views.voter_document, name="voter_document"),
    path("gestione/", views.dashboard, name="dashboard"),
    path("gestione/nuova/", views.election_edit, name="create"),
    path("gestione/<int:pk>/", views.detail, name="detail"),
    path(
        "gestione/<int:pk>/presenze-e-deleghe/",
        assembly_views.manage_assembly,
        name="manage_assembly",
    ),
    path(
        "gestione/<int:pk>/presenze.csv",
        assembly_views.presence_csv,
        name="presence_csv",
    ),
    path(
        "gestione/<int:pk>/deleghe/<int:delegation_pk>/documento/",
        assembly_views.download_delegation_attachment,
        name="delegation_attachment",
    ),
    path(
        "gestione/<int:pk>/deleghe/<int:delegation_pk>/<slug:action>/",
        assembly_views.staff_decision,
        name="staff_delegation",
    ),
    path("gestione/<int:pk>/modifica/", views.election_edit, name="edit"),
    path("gestione/<int:pk>/quesiti/", views.question_edit, name="question_add"),
    path(
        "gestione/<int:pk>/quesiti/<int:qid>/",
        views.question_edit,
        name="question_edit",
    ),
    path("gestione/<int:pk>/elettorato/", views.electorate, name="electorate"),
    path("gestione/<int:pk>/documenti/", views.documents, name="documents"),
    path(
        "gestione/<int:pk>/documenti/<int:doc>/",
        views.admin_document,
        name="admin_document",
    ),
    path("gestione/<int:pk>/anteprima/", views.preview, name="preview"),
    path(
        "gestione/<int:pk>/operazioni/<slug:action>/",
        views.transition,
        name="transition",
    ),
    path("gestione/<int:pk>/elettori/", views.voters, name="voters"),
    path("gestione/<int:pk>/elettori/nuovo/", views.voter_edit, name="voter_add"),
    path(
        "gestione/<int:pk>/elettori/<int:voter_pk>/",
        views.voter_detail,
        name="voter_detail",
    ),
    path(
        "gestione/<int:pk>/elettori/<int:voter_pk>/modifica/",
        views.voter_edit,
        name="voter_edit",
    ),
    path(
        "gestione/<int:pk>/elettori/<int:voter_pk>/rimuovi/",
        views.voter_remove,
        name="voter_remove",
    ),
    path(
        "gestione/<int:pk>/elettori/<int:voter_pk>/link/", views.link, name="copy_link"
    ),
    path(
        "gestione/<int:pk>/elettori/<int:voter_pk>/revoca/", views.rotate, name="rotate"
    ),
    path(
        "gestione/<int:pk>/elettori/<int:voter_pk>/recapiti/",
        views.contact,
        name="contact",
    ),
    path("gestione/<int:pk>/risultati/", views.results, name="results"),
    path(
        "gestione/<int:pk>/rapporto-assembleare/",
        views.assembly_report_page,
        name="assembly_report",
    ),
    path("gestione/<int:pk>/risultati/<slug:kind>/", views.export, name="export"),
]
