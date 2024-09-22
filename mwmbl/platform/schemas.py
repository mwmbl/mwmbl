from ninja import Schema


class Registration(Schema):
    email: str
    username: str
    password: str


class ConfirmEmail(Schema):
    username: str
    email: str
    key: str
