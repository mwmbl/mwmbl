from django.contrib import admin
from django.contrib.admin import ModelAdmin
from django.contrib.auth.admin import UserAdmin

from mwmbl.models import MwmblUser, OldIndex, Curation, FlagCuration, DomainSubmission, ApiKey

admin.site.register(MwmblUser, UserAdmin)
admin.site.register(Curation, ModelAdmin)
admin.site.register(OldIndex, ModelAdmin)
admin.site.register(FlagCuration, ModelAdmin)
admin.site.register(DomainSubmission, ModelAdmin)
admin.site.register(ApiKey, ModelAdmin)
