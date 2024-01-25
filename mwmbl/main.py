import django
import uvicorn
from django.core.management import call_command
import cProfile


def run():
    # django.setup()
    # call_command("collectstatic", "--clear", "--noinput")
    # call_command("migrate")
    uvicorn.run("mwmbl.asgi:application", host="0.0.0.0", port=5000)
    # cProfile.run("mwmbl.asgi:application")


if __name__ == "__main__":
    run()
