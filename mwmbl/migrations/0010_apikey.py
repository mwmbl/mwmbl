# Generated by Django 4.2.14 on 2024-08-09 18:21

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import mwmbl.models


class Migration(migrations.Migration):

    dependencies = [
        ('mwmbl', '0009_alter_domainsubmission_options'),
    ]

    operations = [
        migrations.CreateModel(
            name='ApiKey',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(default=mwmbl.models.random_api_key, max_length=300, unique=True)),
                ('created_on', models.DateTimeField()),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]