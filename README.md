![banner](docs/assets/images/banner_mwmbl.svg)

# Mwmbl - **No ads, no tracking, no cruft, no profit**

[![Matrix](https://img.shields.io/matrix/mwmbl:matrix.org)](https://matrix.to/#/#mwmbl:matrix.org)

Mwmbl is a non-profit, ad-free, free-libre and free-lunch search
engine with a focus on useability and speed. At the moment it is
little more than an idea together with a [proof of concept
implementation](https://mwmbl.org/) of
the web front-end and search technology on a small index.

Our vision is a community working to provide top quality search
particularly for hackers, funded purely by donations.

![mwmbl](https://user-images.githubusercontent.com/1283077/218265959-be4220b4-dcf0-47ab-acd3-f06df0883b52.gif)

Crawling
========

**Update 2022-02-05:** We now have a distributed crawler that runs on
our volunteers' machines! If you have Firefox you can help out by
[installing our
extension](https://addons.mozilla.org/en-GB/firefox/addon/mwmbl-web-crawler/). This
will crawl the web in the background, retrieving one page a second. It
does not use or access any of your personal data. Instead it crawls
the web at random, using the top scoring sites on Hacker News as seed
pages. After extracting a summary of each page, it batches these up
and sends the data to a central server to be stored and indexed.

Why a non-profit search engine?
===============================

The motives of ad-funded search engine are at odds with providing an
optimal user experience. These sites are optimised for ad revenue,
with user experience taking second place. This means that pages are
loaded with ads which are often not clearly distinguished from search
results. Also, eitland on Hacker News
[comments](https://news.ycombinator.com/item?id=29427442):

> Thinking about it it seems logical that for a search engine that
> practically speaking has monopoly both on users and as mattgb points
> out - [to some] degree also on indexing - serving the correct answer
> first is just dumb: if they can keep me going between their search
> results and tech blogs with their ads embedded one, two or five
> times extra that means one, two or five times more ad impressions.

But what about...?
==================

The space of alternative search engines has expanded rapidly in recent
years. Here's a very incomplete list of some that have interested me:

 - [YaCy](https://yacy.net/) - an open source distributed search engine
 - [search.marginalia.nu](https://search.marginalia.nu/) - a search
   engine favouring text-heavy websites
 - [Gigablast](https://gigablast.com/) - a privacy-focused search
   engine whose owner makes money by selling the technology to third
   parties
 - [Brave](https://search.brave.com/)
 - [DuckDuckGo](https://duckduckgo.com/)

Of these, YaCy is the closest in spirit to the idea of a non-profit
search engine. The index is distributed across a peer-to-peer
network. Unfortunately this design decision makes search very slow.

Marginalia Search is fantastic, but it is more of a personal project
than an open source community.

All other search engines that I've come across are for-profit. Please
let me know if I've missed one!

Designing for non-profit
========================

To be a good search engine, we need to store many items, but the cost
of running the engine is at least proportional to the number of items
stored. Our main consideration is thus to reduce the cost per item
stored.

The design is founded on the observation that most items rank for a
small set of terms. In the extreme version of this, where each item
ranks for a single term, the usual inverted index design is grossly
inefficient, since we have to store each term at least twice: once in
the index and once in the item data itself.

Our design is a giant hash map. We have a single store consisting of a
fixed number N of pages. Each page is of a fixed size (currently 4096
bytes to match a page of memory), and consists of a compressed list of
items. Given a term for which we want an item to rank, we compute a
hash of the term, a value between 0 and N - 1. The item is then stored
in the corresponding page.

To retrieve pages, we simply compute the hash of the terms in the user
query and load the corresponding pages, filter the items to those
containing the term and rank the items. Since each page is small, this
can be done very quickly.

Because we compress the list of items, we can rank for more than a
single term and maintain an index smaller than the inverted index
design. Well, that's the theory. This idea has yet to be tested out on
a large scale.

How to contribute
=================

There are lots of ways to help:
 - [Help us crawl the
   web](https://addons.mozilla.org/en-GB/firefox/addon/mwmbl-web-crawler/)
 - [Donate some money](https://opencollective.com/mwmbl) towards
   hosting costs and supporting our volunteers
 - Give feedback/suggestions
 - Help out with development of the engine itself

If you would like to help in any of these or other ways, thank you!
Please join our [Matrix chat
server](https://matrix.to/#/#mwmbl:matrix.org) or email the main
author (email address is in the git commit history).


Development
===========

### Local Testing

For trying out the service locally see [Local Setup](docs/development.md)

### Using Dokku

Note: this method is not recommended as it is more involved, and your index will not have any data in it unless you 
set up a crawler to crawl to your server. You will need to set  up your own Backblaze or S3 equivalent storage, or 
have access to the production keys, which we probably won't give you.

Follow the [deployment instructions](https://github.com/mwmbl/mwmbl/wiki/Deployment)


Frequently Asked Question
=========================

### How do you pronounce "mwmbl"?

Like "mumble". I live in
[Mumbles](https://en.wikipedia.org/wiki/Mumbles), which is spelt
"Mwmbwls" in Welsh. But the intended meaning is "to mumble", as in
"don't search, just mwmbl!"
