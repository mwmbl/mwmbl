Site-Specific Crawling
======================

Site-specific crawling is the ability to have different strategies and parsers for crawling
different sites, particularly those that are high priority for Mwmbl.

# Background

Mwmbl currently is a general purpose search engine, which performs relatively poorly in all
areas. We would like to improve the performance of Mwmbl in a very specific area in order
to be able to deliver value to end-users. The area we have chosen to focus on is resources
for coding, in particular Python coding.

Our goal is to be the best resource for Python coding search, whether for people or AI agents.

We hypothesize that there are several things holding us back from that:
 - Coverage of coding resources, e.g. Python standard library, GitHub repos etc.
 - Knowledge of how to rank results from high-priority sources
 - Extraction of useful meta-data from sources

We propose these ways in which site-specific crawling can help with these:
 - Site-specific discovery, filtering and prioritization of URLs for crawling
 - Site-specific content parsing, enabling us to extract more high-quality
   content from each page (currently we only take the first few characters of each
   page no matter how large it is)
 - Site-specific meta-data extracted from each page - e.g. number of stars/downloads,
   user ratings, code versions


# Index enhancements

We don't currently have a place to store structured content in the index. The current Document class defined in indexer.py
is stored as a JSON list in the index. We need to maintain backward compatibility with this format. To allow this,
we will store items with structured content as a JSON object. To reduce the size of these objects, keys should only
be stored when present and should not have more than two ascii characters, which are mapped to new structured content dataclasses
for each site with readable property names.

To enable backward compatibility with the existing search and API, we need a function to convert from the new classes to
Document objects using a standardised format which can be site-specific. This allows us to leave the current ranking algorithm
and existing API as is for now.

# Site-specific requirements

The initial implementation will focus on just docs.python.org and github.com

## docs.python.org

For every function or method on the page we should extract a structured content item so that these can be individual search results.

Example of a parsed object:

```python
item = DocsPythonOrgStructuredContent(
    url="https://docs.python.org/3/library/gzip.html#gzip.GzipFile.peek",
    definition="peek(n)",
    description="Read n uncompressed bytes without advancing the file position. The number of bytes returned may be more or less than requested.",
    version="3.14.3",
    added_in_version="3.1",
    changed_in_version=[
        ["3.2", "Support for zero-padded and unseekable files was added"],
        ["3.3", "The io.BufferedIOBase.read1() method is now implemented"]
    ]
)
```

Note that we don't need the module or class name since these are implicit in the URL fragment. If the description is too long,
it will need to be truncated to save space in the index, but the maximum size for structured objects should be larger than that
for Document objects since it is only used for high-priority objects.

## github.com

Priority should be for README files within repos.

```python
item = GitHubComStructuredContent(
    url="https://github.com/mwmbl/mwmbl#why-a-non-profit-search-engine",
    stars=1778,
    fork=82,
    watchers=16,
    license="AGPL-3.0",
    title="Why a non-profit search engine?",
    extract="The motives of ad-funded search engines are at odds with providing an optimal user experience. These sites are optimised for ad revenue, with user experience taking second place."
)
```

Each section within the readme should be extracted as a separate item indexed with the URL including the fragment for that paragraph.

