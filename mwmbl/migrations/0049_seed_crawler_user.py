from django.contrib.auth.hashers import make_password
from django.db import migrations

# A frozen copy of mwmbl.models.SEED_CRAWLER_USERNAME: a migration has to keep
# producing the same data forever, so it must not depend on application code.
SEED_CRAWLER_USERNAME = "mwmbl-seed-crawler"


def create_seed_crawler(apps, schema_editor):
    """The user the seed crawl's pages are credited to. Inactive and without a usable
    password, so nobody can sign in as it."""
    MwmblUser = apps.get_model("mwmbl", "MwmblUser")
    MwmblUser.objects.create(username=SEED_CRAWLER_USERNAME, password=make_password(None), is_active=False)


def delete_seed_crawler(apps, schema_editor):
    MwmblUser = apps.get_model("mwmbl", "MwmblUser")
    MwmblUser.objects.filter(username=SEED_CRAWLER_USERNAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("mwmbl", "0048_crawleragreement"),
    ]

    operations = [
        migrations.RunPython(create_seed_crawler, delete_seed_crawler),
    ]
