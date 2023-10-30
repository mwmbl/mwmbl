from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from mwmbl.search_setup import ranker


@login_required
def profile(request):
    return render(request, 'profile.html')


def search_results(request):
    query = request.GET["query"]
    results = ranker.search(query)
    return render(request, "results.html", {"results": results})
