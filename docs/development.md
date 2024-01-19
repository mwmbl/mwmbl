### Local Testing

This will run against a local test database without running background
tasks to update batches etc.

You will need a PostgreSQL database.
If you also want to use the crawler you additionally will need to install redis.

Check that you are running Python 3.10
If not then either install it from your distro or from [python.org](https://python.org/downloads)

1. Install [poetry](https://python-poetry.org/docs/#installation)
2. Run `poetry install` to install dependencies
3. Run `poetry shell` in the root directory to enter the virtual environment

Now to build the Vitejs/htmx frontend install [node](https://nodejs.org)
Specifically you will need Nodejs 16 later
1. In the front-end directory run `npm install` then `npm run build`

Set some environment variables:
1. First set the config `$ export DJANGO_SETTINGS_MODULE=mwmbl.settings_dev`
2. For the database set `$ export DATABASE_URL="postgres://yourusername:yourpassword@localhost/yourdbname"`
Where you replace username, password and database name respectivley with your own credentials.

Note that you can also directly edit the config in `mwmbl/setttings*.py` instead of the environment variables.

Finally:
1. Run the migrations `python3 manage.py migrate`
2. To start the service run `python3 manage.py runserver`
