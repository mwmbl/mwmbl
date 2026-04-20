from django.db import migrations


def set_crawl_scope(apps, schema_editor):
    """Set scopes=["crawl"] on all existing ApiKey rows that have no scopes."""
    ApiKey = apps.get_model("mwmbl", "ApiKey")
    ApiKey.objects.filter(scopes=[]).update(scopes=["crawl"])


class Migration(migrations.Migration):

    dependencies = [
        ('mwmbl', '0016_apikey_scopes_name_mwmbluser_tier'),
    ]

    operations = [
        migrations.RunPython(set_crawl_scope, migrations.RunPython.noop),
    ]
