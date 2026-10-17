import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("mwmbl", "0045_volunteermembership"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserNotification",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "kind",
                    models.CharField(
                        choices=[("volunteer_membership_granted", "Volunteer membership granted")], max_length=40
                    ),
                ),
                ("data", models.JSONField(default=dict)),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("dismissed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notifications",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "indexes": [models.Index(fields=["user", "dismissed_at"], name="mwmbl_usern_user_id_980c84_idx")],
            },
        ),
    ]
