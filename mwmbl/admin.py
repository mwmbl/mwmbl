from django import forms
from django.contrib import admin
from django.contrib.admin import ModelAdmin
from django.contrib.auth.admin import UserAdmin
from django.urls import path

from mwmbl.admin_views import purge_blacklisted_domains_view
from mwmbl.models import MwmblUser, OldIndex, Curation, FlagCuration, DomainSubmission, ApiKey, MarketingConsent, UserBilling, generate_api_key


_default_get_urls = admin.site.get_urls


def _get_urls():
    return [
        path("purge-blacklisted-domains/", admin.site.admin_view(purge_blacklisted_domains_view),
             name="purge_blacklisted_domains"),
    ] + _default_get_urls()


admin.site.get_urls = _get_urls


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


class UserBillingAdmin(ModelAdmin):
    list_display = ("user", "max_monthly_spend_cents", "polar_customer_id", "current_period_end", "cancel_at_period_end")
    readonly_fields = ("polar_customer_id", "polar_subscription_id", "current_period_end")
    search_fields = ("user__username", "user__email", "polar_customer_id")


admin.site.register(ApiKey, ApiKeyAdmin)
admin.site.register(MarketingConsent, MarketingConsentAdmin)
admin.site.register(UserBilling, UserBillingAdmin)
