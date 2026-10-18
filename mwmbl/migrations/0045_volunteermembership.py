import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("mwmbl", "0044_mwmbluser_combined_search_enabled"),
    ]

    operations = [
        migrations.CreateModel(
            name="VolunteerMembership",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("tier", models.CharField(choices=[("pollinator", "Pollinator")], max_length=20)),
                ("valid_until", models.DateField()),
                ("started", models.DateTimeField(auto_now_add=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="volunteer_memberships",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "unique_together": {("user", "tier")},
            },
        ),
    ]
