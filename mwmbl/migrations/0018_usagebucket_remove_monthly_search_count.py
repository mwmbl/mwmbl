from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('mwmbl', '0017_backfill_apikey_scopes'),
    ]

    operations = [
        migrations.CreateModel(
            name='UsageBucket',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('year', models.IntegerField()),
                ('month', models.IntegerField()),
                ('count', models.IntegerField(default=0)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'unique_together': {('user', 'year', 'month')},
            },
        ),
        migrations.RemoveField(
            model_name='mwmbluser',
            name='monthly_search_count',
        ),
    ]
