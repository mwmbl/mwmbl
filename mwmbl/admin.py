from django.contrib.admin import ModelAdmin
from django.contrib.auth.admin import UserAdmin
from django.contrib import admin

from mwmbl.models import MwmblUser, UserCuration

admin.site.register(MwmblUser, UserAdmin)
admin.site.register(UserCuration, ModelAdmin)
