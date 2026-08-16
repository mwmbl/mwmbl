"""
Data migration cleaning up duplicate periodic Task rows created by past
deploys racing past the non-atomic scheduling check in
MwmblConfig._schedule_background_tasks() (fixed alongside this migration).

For each periodic task name, keeps a single unlocked row and deletes the
rest. Locked (in-flight) rows are left alone so we never interfere with a
task that's currently running.
"""
from django.db import migrations

TASK_NAMES = [
    "mwmbl.background.sync_search_counts",
    "mwmbl.background.report_usage_to_polar",
]


def dedupe_task_schedules(apps, schema_editor):
    Task = apps.get_model("background_task", "Task")
    for task_name in TASK_NAMES:
        duplicates = list(
            Task.objects.filter(task_name=task_name, locked_by__isnull=True).order_by("id")
        )
        # Keep the oldest row, delete the rest.
        for task in duplicates[1:]:
            task.delete()


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("mwmbl", "0030_remove_mwmbluser_tier_usagebucket_reported_overage_and_more"),
        ("background_task", "0002_auto_20170927_1109"),
    ]

    operations = [
        migrations.RunPython(dedupe_task_schedules, noop_reverse),
    ]
