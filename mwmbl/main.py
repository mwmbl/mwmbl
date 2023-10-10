import uvicorn


def run():
    uvicorn.run("mwmbl.asgi:application", host="0.0.0.0", port=5000)


if __name__ == "__main__":
    run()
