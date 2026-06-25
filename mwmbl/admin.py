from django import forms
from django.contrib import admin
from django.contrib.admin import ModelAdmin
from django.contrib.auth.admin import UserAdmin

from mwmbl.models import MwmblUser, OldIndex, Curation, FlagCuration, DomainSubmission, ApiKey, MarketingConsent, generate_api_key


class ApiKeyForm(forms.ModelForm):
    scopes = forms.MultipleChoiceField(
        choices=ApiKey.Scope.choices,
        widget=forms.CheckboxSelectMultiple,
        required=False,
    )

    class Meta:
        model = ApiKey
        fields = "__all__"


class ApiKeyAdmin(ModelAdmin):
    form = ApiKeyForm
    list_display = ("user", "name", "scopes", "created_on")
    readonly_fields = ("key", "created_on")
    fields = ("user", "name", "scopes", "key", "created_on")
    _pending_raw_key = None

    def save_model(self, request, obj, form, change):
        if not change:
            raw_key, key_hash = generate_api_key()
            obj.key = key_hash
            self._pending_raw_key = raw_key
        super().save_model(request, obj, form, change)
        if self._pending_raw_key:
            self.message_user(
                request,
                f"API key created. Save this key now — it will not be shown again: {self._pending_raw_key}",
                level="warning",
            )
            self._pending_raw_key = None

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return self.readonly_fields + ("user",)
        return self.readonly_fields


admin.site.register(MwmblUser, UserAdmin)
admin.site.register(Curation, ModelAdmin)
admin.site.register(OldIndex, ModelAdmin)
admin.site.register(FlagCuration, ModelAdmin)
admin.site.register(DomainSubmission, ModelAdmin)
class MarketingConsentAdmin(ModelAdmin):
    list_display = ("user", "source", "opted_in", "timestamp")
    list_filter = ("source", "opted_in")
    readonly_fields = ("user", "source", "opted_in", "timestamp")


admin.site.register(ApiKey, ApiKeyAdmin)
admin.site.register(MarketingConsent, MarketingConsentAdmin)
