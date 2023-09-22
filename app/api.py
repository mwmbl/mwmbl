from ninja import NinjaAPI

api = NinjaAPI(version="1.0.0")


@api.get("/hello")
def hello(request):
    return {"response": "Hello world"}
