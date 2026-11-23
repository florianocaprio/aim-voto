from django.urls import path

from . import views

app_name = "members"

urlpatterns = [
    path("", views.upload, name="import"),
    path("modello.csv", views.template_csv, name="template-csv"),
    path("modello.xlsx", views.template_xlsx, name="template-xlsx"),
    path("demo.csv", views.demo_csv, name="demo-csv"),
    path("<uuid:batch_id>/associa/", views.mapping, name="mapping"),
    path("<uuid:batch_id>/anteprima/", views.preview, name="preview"),
]
