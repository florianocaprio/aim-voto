import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Member",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("external_id", models.CharField(max_length=64, unique=True, verbose_name="ID socio")),
                ("first_name", models.CharField(max_length=100, verbose_name="Nome")),
                ("last_name", models.CharField(max_length=100, verbose_name="Cognome")),
                ("email", models.EmailField(max_length=254, verbose_name="Email")),
                ("phone", models.CharField(blank=True, max_length=32, verbose_name="Cellulare")),
                ("section", models.CharField(max_length=100, verbose_name="Sezione")),
            ],
            options={"ordering": ("last_name", "first_name", "external_id")},
        ),
        migrations.CreateModel(
            name="ImportBatch",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("headings", models.JSONField(default=list)),
                ("records", models.JSONField(default=list)),
                ("blank_rows", models.PositiveIntegerField(default=0)),
                ("mapping", models.JSONField(default=dict)),
                ("summary", models.JSONField(default=dict)),
                (
                    "owner",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL),
                ),
            ],
        ),
    ]
