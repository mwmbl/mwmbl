"""
URL configuration for app project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include

from mwmbl.api import api_v1
from mwmbl.views import home_fragment, fetch_url, index, page_history

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/v1/', api_v1.urls),
    path('accounts/', include('allauth.urls')),

    path('', index, name="home"),
    path('app/home/', home_fragment, name="home"),
    path('app/fetch/', fetch_url, name="fetch_url"),
    path('app/history/', page_history, name="history"),
]
