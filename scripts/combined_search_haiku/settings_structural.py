from mwmbl.settings_dev import *  # noqa: F401,F403

# The structural-fixes scripts read the live index over HTTP and need neither the database
# nor the background tasks that apps.ready() schedules through Redis.
HAS_DATABASE = False
