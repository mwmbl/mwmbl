from django.db import migrations, models
import django.contrib.postgres.fields


class Migration(migrations.Migration):

    dependencies = [
        ('mwmbl', '0015_alter_searchresultvote_vote_type'),
    ]

    operations = [
        # ApiKey: add name field
        migrations.AddField(
            model_name='apikey',
            name='name',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
        # ApiKey: add scopes ArrayField
        migrations.AddField(
            model_name='apikey',
            name='scopes',
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(
                    choices=[('crawl', 'Crawl'), ('search', 'Search')],
                    max_length=20,
                ),
                default=list,
                size=None,
            ),
        ),
        # ApiKey: change created_on to auto_now_add
        # We use AlterField; existing rows already have a value so this is safe.
        migrations.AlterField(
            model_name='apikey',
            name='created_on',
            field=models.DateTimeField(auto_now_add=True),
            preserve_default=False,
        ),
        # MwmblUser: add tier field
        migrations.AddField(
            model_name='mwmbluser',
            name='tier',
            field=models.CharField(
                choices=[('free', 'Free'), ('starter', 'Starter'), ('pro', 'Pro')],
                default='free',
                max_length=20,
            ),
        ),
        # MwmblUser: add monthly_search_count field
        migrations.AddField(
            model_name='mwmbluser',
            name='monthly_search_count',
            field=models.IntegerField(default=0),
        ),
    ]
