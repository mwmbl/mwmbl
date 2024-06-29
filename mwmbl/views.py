import re
from dataclasses import asdict
from datetime import datetime
from logging import getLogger
from typing import Optional
from urllib.parse import urlencode

import justext
import requests
from django.conf import settings
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.forms import ModelForm, ModelChoiceField, RadioSelect, CharField
from django.http import HttpResponseBadRequest
from django.shortcuts import render, redirect
from django.views.decorators.http import require_http_methods
from django.views.generic import DetailView, ListView
from justext.core import html_to_dom, ParagraphMaker, classify_paragraphs, revise_paragraph_classification, \
    LENGTH_LOW_DEFAULT, STOPWORDS_LOW_DEFAULT, MAX_LINK_DENSITY_DEFAULT, NO_HEADINGS_DEFAULT, LENGTH_HIGH_DEFAULT, \
    STOPWORDS_HIGH_DEFAULT, MAX_HEADING_DISTANCE_DEFAULT, DEFAULT_ENCODING, DEFAULT_ENC_ERRORS, preprocessor
from requests.exceptions import RequestException

from mwmbl.crawler.app import stats_manager
from mwmbl.models import Curation, FlagCuration, DomainSubmission
from mwmbl.search_setup import ranker, index_path
from mwmbl.settings import NUM_EXTRACT_CHARS
from mwmbl.tinysearchengine.indexer import Document, DocumentState, TinyIndex
from mwmbl.tinysearchengine.rank import fix_document_state
from mwmbl.tokenizer import tokenize
from mwmbl.utils import add_term_infos, parse_url, validate_domain

MAX_CURATED_SCORE = 1_111_111.0


logger = getLogger(__name__)


def justext_with_dom(html_text, stoplist, length_low=LENGTH_LOW_DEFAULT,
        length_high=LENGTH_HIGH_DEFAULT, stopwords_low=STOPWORDS_LOW_DEFAULT,
        stopwords_high=STOPWORDS_HIGH_DEFAULT, max_link_density=MAX_LINK_DENSITY_DEFAULT,
        max_heading_distance=MAX_HEADING_DISTANCE_DEFAULT, no_headings=NO_HEADINGS_DEFAULT,
        encoding=None, default_encoding=DEFAULT_ENCODING,
        enc_errors=DEFAULT_ENC_ERRORS):
    """
    Converts an HTML page into a list of classified paragraphs. Each paragraph
    is represented as instance of class ˙˙justext.paragraph.Paragraph˙˙.
    """
    dom = html_to_dom(html_text, default_encoding, encoding, enc_errors)

    titles = dom.xpath("//title")
    title = titles[0].text if len(titles) > 0 else None

    dom = preprocessor(dom)

    paragraphs = ParagraphMaker.make_paragraphs(dom)

    classify_paragraphs(paragraphs, stoplist, length_low, length_high,
        stopwords_low, stopwords_high, max_link_density, no_headings)
    revise_paragraph_classification(paragraphs, max_heading_distance)

    return paragraphs, title


def index(request):
    activity, query, results = _get_results_and_activity(request)
    return render(request, "index.html", {
        "results": results,
        "query": query,
        "user": request.user,
        "activity": activity,
        "footer_links": settings.FOOTER_LINKS,
    })


def home_fragment(request):
    activity, query, results = _get_results_and_activity(request)
    response = render(request, "home.html", {
        "results": results,
        "query": query,
        "activity": activity,
    })

    # Encode the new query string
    if query:
        new_query_string = urlencode({"q": query}, doseq=True)
        new_url = "/?" + new_query_string
    else:
        new_url = "/"
    response["HX-Replace-Url"] = new_url
    return response


def _get_results_and_activity(request):
    query = request.GET.get("q")
    if query:
        # There may be extra results in the request that we need to add in
        # format is ?enhanced=google&title=title1&url=url1&extract=extract1&title=title2&url=url2&extract=extract2
        # source = request.GET.get("enhanced", "unknown")
        titles = request.GET.getlist(f"title")
        urls = request.GET.getlist(f"url")
        extracts = request.GET.getlist(f"extract")

        term = " ".join(tokenize(query))

        # For now, we only support the Google source
        additional_results = [
            Document(title=title, url=url, extract=extract, score=100.0 * 2 ** -i, term=term, state=DocumentState.FROM_GOOGLE)
            for i, (title, url, extract) in enumerate(zip(titles, urls, extracts))
        ]

        results = ranker.search(query, additional_results=additional_results)
        activity = None
    else:
        results = None
        activity = Curation.objects.filter(flag_curation_set__isnull=True).order_by("-timestamp")[:8]
    return activity, query, results


@login_required
@require_http_methods(["POST"])
def add_url(request):
    new_url = request.POST["new_url"]
    query = request.POST["query"]

    try:
        response = requests.get(new_url, timeout=5)
    except RequestException:
        return HttpResponseBadRequest("Could not fetch URL")

    paragraphs, title = justext_with_dom(response.content, justext.get_stoplist("English"))
    good_paragraphs = [p for p in paragraphs if p.class_type == 'good']

    extract = ' '.join([p.text for p in good_paragraphs])
    if len(extract) > NUM_EXTRACT_CHARS:
        extract = extract[:NUM_EXTRACT_CHARS - 1] + '…'

    term = " ".join(tokenize(query))
    result = Document(title=title, url=new_url, extract=extract, score=0.0, term=term, state=DocumentState.FROM_USER_APPROVED)

    documents = _get_documents(request, term)
    reranked_documents = _insert_document(documents, result)
    curation = _get_curation(request, query, documents, reranked_documents)

    _save_to_index(query, reranked_documents)

    return render(request, "home.html", {
        "results": reranked_documents,
        "query": query,
        "activity": None,
        "curation": curation,
    })


class DomainSubmissionForm(ModelForm):
    class Meta:
        model = DomainSubmission
        fields = ["name"]

    name = CharField(validators=[validate_domain])

    def clean_name(self):
        """
        Domain names or URLs are allowed. If a URL is submitted, just extract the domain.
        """
        original_name = self.cleaned_data["name"]
        try:
            domain = parse_url(original_name).netloc
            if domain is not None:
                return domain
        except ValueError:
            pass
        return original_name


class DomainSubmissionApprovalForm(ModelForm):
    class Meta:
        model = DomainSubmission
        fields = ["status", "rejection_reason", "rejection_detail"]


@login_required
def submit_domain(request):
    if request.method == "POST":
        form = DomainSubmissionForm(request.POST)
        if form.is_valid():
            domain_submission = form.save(commit=False)
            domain_submission.submitted_by = request.user
            domain_submission.submitted_on = datetime.utcnow()
            domain_submission.save()
            return redirect("domain_submissions")
    else:
        form = DomainSubmissionForm()
    return render(request, "mwmbl/domain_submission.html", {"form": form})


class DomainSubmissionListView(ListView):
    model = DomainSubmission
    template_name = "mwmbl/domain_submission_list.html"

    def get_queryset(self):
        return DomainSubmission.objects.all().order_by("-submitted_on")


def switch_state(state: Optional[DocumentState]) -> Optional[DocumentState]:
    if state is None:
        return DocumentState.ORGANIC_APPROVED
    if state == DocumentState.FROM_GOOGLE:
        return DocumentState.FROM_GOOGLE_APPROVED
    if state == DocumentState.FROM_USER:
        return DocumentState.FROM_USER_APPROVED
    if state == DocumentState.FROM_GOOGLE_APPROVED:
        return DocumentState.FROM_GOOGLE
    if state == DocumentState.FROM_USER_APPROVED:
        return DocumentState.FROM_USER
    if state == DocumentState.ORGANIC_APPROVED:
        return None
    raise ValueError(f"Unexpected state {repr(state)}")


@login_required
@require_http_methods(["POST"])
def approve(request):
    approve_url = request.POST.get("approve_url")
    query = request.POST.get("query")

    term = " ".join(tokenize(query))
    documents = _get_documents(request, term)

    # The approved Document should be pushed below the last Document with status > 0
    # If there are no such documents, push it to the top

    document_to_approve = documents[approve_url]
    approved_document = Document(
        title=document_to_approve.title,
        url=document_to_approve.url,
        extract=document_to_approve.extract,
        score=document_to_approve.score,
        term=document_to_approve.term,
        state=switch_state(document_to_approve.state),
    )

    reranked_documents = _insert_document(documents, approved_document)
    curation = _get_curation(request, query, documents, reranked_documents)

    _save_to_index(query, reranked_documents)

    response = render(request, "home.html", {
        "results": reranked_documents,
        "query": query,
        "activity": None,
        "curation": curation,
    })

    return response


@login_required
@require_http_methods(["POST"])
def revert_current_curation(request):
    curation_id = request.POST.get("curation_id")
    curation = Curation.objects.get(id=curation_id)
    _revert_curation(curation)

    # Delete the curation
    curation.delete()

    original_documents_unfixed = [Document(**doc) for doc in curation.original_results]
    original_documents = [fix_document_state(doc) for doc in original_documents_unfixed]
    return render(request, "home.html", {
        "results": original_documents,
        "query": (curation.query),
        "activity": None,
        "curation": None,
    })


def _revert_curation(curation):
    with TinyIndex(Document, index_path, 'w') as indexer:
        term = " ".join(tokenize(curation.query))
        documents = [Document(**doc) for doc in curation.original_index_results]

        page_index = indexer.get_key_page_index(term)
        existing_documents = indexer.get_page(page_index)
        other_term_documents = [doc for doc in existing_documents if doc.term != term]

        # Replace all existing documents for the term with the original documents
        all_documents = documents + other_term_documents

        indexer.store_in_page(page_index, all_documents)


def _get_curation(request, query, documents, reranked_documents):
    curation_id = request.POST.get("curation_id")
    reranked_document_dicts = [asdict(d) for d in reranked_documents]
    if curation_id is not None:
        curation = Curation.objects.get(id=curation_id)
        curation.new_results = reranked_document_dicts
        curation.num_changes += 1
        curation.save()
    else:
        user = request.user
        if not user.is_authenticated:
            user = None

        with TinyIndex(Document, index_path, 'r') as indexer:
            tokens = tokenize(query)
            term = " ".join(tokens)
            original_index_results = [doc for doc in indexer.retrieve(term) if doc.term == term]

        curation = Curation(
            user=user,
            timestamp=datetime.utcnow(),
            query=query,
            original_index_results=[asdict(d) for d in original_index_results],
            original_results=[asdict(d) for d in documents.values()],
            new_results=reranked_document_dicts,
            num_changes=1,
        )
        curation.save()
    return curation


def _insert_document(documents, approved_document):
    reranked_documents = []
    inserted_approved = False
    for document in documents.values():
        if document.url == approved_document.url:
            continue

        if (document.state is None or document.state < DocumentState.ORGANIC_APPROVED) and not inserted_approved:
            reranked_documents.append(approved_document)
            inserted_approved = True

        reranked_documents.append(document)
    if not inserted_approved:
        reranked_documents.append(approved_document)
    return reranked_documents


def _get_documents(request, term: str):
    urls = request.POST.getlist("url")
    titles = request.POST.getlist("title")
    extracts = request.POST.getlist("extract")
    states = request.POST.getlist("state")
    scores = request.POST.getlist("score")
    assert len(urls) == len(titles) == len(extracts) == len(states) == len(scores)
    documents = {}
    for url, title, extract, state, score in zip(urls, titles, extracts, states, scores):
        try:
            state_enum = DocumentState(int(state))
        except ValueError:
            state_enum = None
        documents[url] = Document(
            title=title, url=url, extract=extract, score=float(score), term=term, state=state_enum)
    return documents


def _save_to_index(query: str, new_results: list[Document]):
    with TinyIndex(Document, index_path, 'w') as indexer:
        term = " ".join(tokenize(query))
        documents = [
            Document(
                title=result.title,
                url=result.url,
                extract=result.extract,
                score=MAX_CURATED_SCORE - i,
                term=term,
                state=result.state,
            )
            for i, result in enumerate(new_results)
            if result.state is not None and result.state >= DocumentState.ORGANIC_APPROVED
        ]

        page_index = indexer.get_key_page_index(term)
        existing_documents_no_terms = indexer.get_page(page_index)
        existing_documents = add_term_infos(existing_documents_no_terms, indexer, page_index)
        new_urls = {doc.url for doc in documents}
        other_documents = [doc for doc in existing_documents if doc.url not in new_urls]
        logger.info(f"Found {len(other_documents)} other documents for term {term} at page {page_index} "
                    f"with terms { {doc.term for doc in other_documents} }")

        # Update state for other documents
        states = {doc.url: doc.state for doc in new_results}
        for doc in other_documents:
            doc.state = states.get(doc.url, doc.state)

        all_documents = documents + other_documents
        logger.info(f"Storing {len(all_documents)} documents at page {page_index}")
        indexer.store_in_page(page_index, all_documents)

    return {"curation": "ok"}


def _get_document_state(validated: bool, source: str) -> Optional[DocumentState]:
    if validated:
        if source.lower() == "user":
            return DocumentState.FROM_USER_APPROVED
        elif source.lower() == "google":
            return DocumentState.FROM_GOOGLE_APPROVED
        else:
            return DocumentState.ORGANIC_APPROVED
    elif source.lower() == "user":
        return DocumentState.FROM_USER
    elif source.lower() == "google":
        return DocumentState.FROM_GOOGLE
    else:
        return None


class CurationsView(ListView):
    paginate_by = 40
    model = Curation
    template_name = "mwmbl/curations.html"

    def get_queryset(self):
        return Curation.objects.prefetch_related('flag_curation_set').all().order_by("-timestamp")


class CurationDetailView(DetailView):
    model = Curation

    def get_context_data(self, **kwargs):
        flags = FlagCuration.objects.filter(curation=self.object, status="PENDING")
        return super().get_context_data(flags=flags, **kwargs)


class CurationFlagForm(ModelForm):
    class Meta:
        model = FlagCuration
        fields = ["flag", "reason"]
        widgets = {
            "flag": RadioSelect(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["flag"].widget.attrs["class"] = "form-select"


@login_required
def flag_curation(request, curation_id):
    if request.method == "POST":
        form = CurationFlagForm(request.POST)
        if form.is_valid():
            curation = form.save(commit=False)
            curation.user = request.user
            curation.timestamp = datetime.now()
            curation.curation_id = curation_id
            curation.save()
            return render(request, "mwmbl/flag_curation_success.html")
    else:
        form = CurationFlagForm()
    return render(request, "mwmbl/flag_curation.html", {"form": form, "curation_id": curation_id})


@login_required
@permission_required("mwmbl.change_flag_status")
def flag_curation_update(request, flag_curation_id):
    new_status = request.POST.get("status")
    if new_status not in FlagCuration.FLAG_STATUS:
        return HttpResponseBadRequest("Invalid status")
    flag_curation = FlagCuration.objects.get(id=flag_curation_id)
    flag_curation.status = new_status
    flag_curation.save()

    # If the flag has been accepted, revert the curation
    if new_status == "ACCEPTED":
        _revert_curation(flag_curation.curation)

    flags = FlagCuration.objects.filter(curation=flag_curation.curation.id, status="PENDING")
    return render(request, "mwmbl/flags.html", context={"flags": flags})


class CurationFlagListView(LoginRequiredMixin, ListView):
    model = FlagCuration
    template_name = "mwmbl/flag_curation_list.html"

    def get_queryset(self):
        return FlagCuration.objects.filter(status="PENDING").order_by("-timestamp")


def domains_view(request):
    domain_stats = stats_manager.get_domain_stats()
    return render(request, "mwmbl/domains.html", {"domain_stats": domain_stats})


def domain_view(request, domain):
    if request.method == "POST":
        if request.user.has_perm("mwmbl.change_domain_submission_status"):
            instance_id = request.POST.get("id")
            if instance_id is not None:
                instance = DomainSubmission.objects.get(id=instance_id)
                form = DomainSubmissionApprovalForm(request.POST, instance=instance)
                if form.is_valid():
                    form.save()

    domain_stats = stats_manager.get_stats_for_domain(domain)
    domain_submissions = DomainSubmission.objects.filter(name=domain).order_by("-submitted_on")

    # Add a form if the user is a moderator
    if request.user.has_perm("mwmbl.change_domain_submission_status"):
        pending_submissions = DomainSubmission.objects.filter(name=domain, status="PENDING").order_by("-submitted_on")
        forms = [DomainSubmissionApprovalForm(instance=submission) for submission in pending_submissions]
    else:
        forms = []

    context = {
        "domain_stats": domain_stats,
        "domain_submissions": domain_submissions,
        "forms": forms,
    }

    return render(request, "mwmbl/domain.html", context)
