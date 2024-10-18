from ninja import Schema, ModelSchema

from mwmbl.models import DomainSubmission


class Registration(Schema):
    email: str
    username: str
    password: str


class ConfirmEmail(Schema):
    username: str
    email: str
    key: str


class DomainSubmissionSchema(ModelSchema):
    class Meta:
        model = DomainSubmission
        fields = ["id", "name", "submitted_by", "submitted_on", "status", "rejection_reason", "rejection_detail"]


class UpdateDomainSubmission(ModelSchema):
    class Meta:
        model = DomainSubmission
        fields = ["status", "rejection_reason", "rejection_detail"]
