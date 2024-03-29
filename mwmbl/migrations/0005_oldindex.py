# Generated by Django 4.2.6 on 2024-03-05 15:16

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mwmbl', '0004_curation_original_index_results'),
    ]

    operations = [
        migrations.CreateModel(
            name='OldIndex',
            fields=[
                ('index_path', models.CharField(max_length=300, primary_key=True, serialize=False)),
                ('index_total_pages', models.IntegerField()),
                ('last_copied_time', models.DateTimeField()),
                ('last_page_copied', models.IntegerField()),
            ],
        ),
    ]
