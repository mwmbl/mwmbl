from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("mwmbl", "0051_device_results"),
    ]

    operations = [
        migrations.AddField(
            model_name="device",
            name="crawler_version",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
