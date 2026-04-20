from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mwmbl', '0019_add_userbilling'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='usagebucket',
            index=models.Index(fields=['year', 'month'], name='mwmbl_usage_year_mo_idx'),
        ),
    ]
