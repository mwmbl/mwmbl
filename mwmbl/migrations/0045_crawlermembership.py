import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("mwmbl", "0044_mwmbluser_combined_search_enabled"),
    ]

    operations = [
        migrations.CreateModel(
            name="CrawlerMembership",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("valid_until", models.DateField()),
                ("earned", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="crawler_membership",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
    ]
