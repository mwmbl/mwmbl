import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("mwmbl", "0050_crawlererror"),
    ]

    operations = [
        migrations.AddField(
            model_name="device",
            name="total_results",
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="device",
            name="hidden",
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name="DeviceSubmission",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("submitted_at", models.DateTimeField()),
                ("num_results", models.IntegerField()),
                (
                    "device",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="submissions", to="mwmbl.device"
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="devicesubmission",
            index=models.Index(fields=["device", "submitted_at"], name="mwmbl_devic_device__3e624e_idx"),
        ),
    ]
