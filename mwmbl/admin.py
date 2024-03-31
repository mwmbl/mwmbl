from django.contrib import admin
from django.contrib.admin import ModelAdmin
from django.contrib.auth.admin import UserAdmin

from mwmbl.models import MwmblUser, OldIndex, Curation

admin.site.register(MwmblUser, UserAdmin)
admin.site.register(Curation, ModelAdmin)
admin.site.register(OldIndex, ModelAdmin)
