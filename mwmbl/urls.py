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

import mwmbl.crawler.app as crawler
from mwmbl.search_setup import queued_batches, ranker, batch_cache
from mwmbl.tinysearchengine import search
from mwmbl.views import home_fragment, add_url, index, approve, revert_current_curation, CurationDetailView, \
    flag_curation, CurationFlagListView, flag_curation_update, domains_view, domain_view, CurationsView, submit_domain, \
    DomainSubmissionListView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('allauth.urls')),

    path('', index, name="index"),
    path('app/home/', home_fragment, name="home"),
    path('app/add/', add_url, name="add_url"),
    path('app/domain-submissions/', DomainSubmissionListView.as_view(), name="domain_submissions"),
    path('app/domain-submissions/new', submit_domain, name="submit_domain"),
    path('app/approve/', approve, name="approve"),
    path('app/revert-current/', revert_current_curation, name="revert_current"),
    path('app/curations/', CurationsView.as_view(), name="curations"),
    path('app/curations/<int:pk>/', CurationDetailView.as_view(), name="curation"),
    path('app/curations/<int:curation_id>/flag/', flag_curation, name="flag_curation"),
    path('app/flags/curation', CurationFlagListView.as_view(), name="flag_curation_list"),
    path('app/flags/curation/<int:flag_curation_id>/update', flag_curation_update, name="flag_curation_update"),
    path('app/domains/', domains_view, name="domains"),
    path('app/domains/<str:domain>/', domain_view, name="domain"),

    # TODO: this is the old API, deprecated and to be removed once all clients have moved over
    path("search/", search.create_router(ranker, "0.1").urls),
    path("crawler/", crawler.create_router(batch_cache=batch_cache, queued_batches=queued_batches, version="0.1").urls),

    # New API
    path("api/v1/search/", search.create_router(ranker, "1.0.0").urls),
    path("api/v1/crawler/", crawler.create_router(batch_cache=batch_cache, queued_batches=queued_batches, version="1.0.0").urls),
]
