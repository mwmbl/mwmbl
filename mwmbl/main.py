import django
import uvicorn
from django.core.management import call_command


def run():
    django.setup()
    call_command('migrate')
    uvicorn.run("mwmbl.asgi:application", host="0.0.0.0", port=5000)


if __name__ == "__main__":
    run()
