from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("mwmbl", "0041_usagebucket_combined_search"),
    ]

    # Existing users get False, so nobody already registered has Combined Search switched on for them;
    # the second step makes it on for accounts created from now on.
    operations = [
        migrations.AddField(
            model_name="mwmbluser",
            name="combined_search_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name="mwmbluser",
            name="combined_search_enabled",
            field=models.BooleanField(default=True),
        ),
    ]
