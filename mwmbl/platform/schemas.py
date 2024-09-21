from ninja import Schema


class Registration(Schema):
    email: str
    username: str
    password: str
