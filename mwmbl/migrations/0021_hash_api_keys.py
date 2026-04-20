import hashlib

from django.db import migrations, models


def hash_existing_keys(apps, schema_editor):
    ApiKey = apps.get_model('mwmbl', 'ApiKey')
    for api_key in ApiKey.objects.all():
        api_key.key = hashlib.sha256(api_key.key.encode()).hexdigest()
        api_key.save(update_fields=['key'])


class Migration(migrations.Migration):

    dependencies = [
        ('mwmbl', '0020_usagebucket_year_month_index'),
    ]

    operations = [
        migrations.RunPython(hash_existing_keys, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='apikey',
            name='key',
            field=models.CharField(max_length=64, unique=True),
        ),
    ]
