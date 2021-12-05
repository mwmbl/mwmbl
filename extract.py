"""
Extract content from HTML files and store it as compressed JSON
"""

import json
import os
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import spacy as spacy
from justext import get_stoplist
from justext.core import LENGTH_LOW_DEFAULT, LENGTH_HIGH_DEFAULT, STOPWORDS_LOW_DEFAULT, \
    STOPWORDS_HIGH_DEFAULT, MAX_LINK_DENSITY_DEFAULT, NO_HEADINGS_DEFAULT, \
    MAX_HEADING_DISTANCE_DEFAULT, DEFAULT_ENCODING, DEFAULT_ENC_ERRORS, preprocessor, html_to_dom, \
    ParagraphMaker, classify_paragraphs, revise_paragraph_classification
from langdetect import detect
from lxml.etree import ParserError
from pyspark.sql import DataFrame
from pyspark.sql import SparkSession, SQLContext
from pyspark.sql.types import StructType, StructField, StringType, LongType
from pyspark.sql.functions import expr, col
from pyspark import SparkFiles



import boto3
from warcio import ArchiveIterator


OUTPUT_PATH = 's3://tinysearch/outputs/index'

MAX_URI_LENGTH = 150
NUM_CHARS_TO_ANALYSE = 1000
NUM_TITLE_CHARS = 65
NUM_EXTRACT_CHARS = 155
NUM_PAGES = 1024
MAX_RESULTS_PER_HASH = 200
PAGE_SIZE = 4096


nlp = spacy.load("en_core_web_sm", disable=['lemmatizer', 'ner'])


index_schema = StructType([
    StructField("term_hash", LongType(), False),
    StructField("data", StringType(), False),
    StructField("top", StringType(), False),
])

spark = SparkSession \
    .builder \
    .appName("Python Spark SQL basic example") \
    .config("spark.some.config.option", "some-value") \
    .getOrCreate()


def run():
    sqlc = SQLContext(sparkContext=spark)

    df = spark.read.load('s3://commoncrawl/cc-index/table/cc-main/warc/')
    df.createOrReplaceTempView('ccindex')
    sqldf = spark.sql('''SELECT url, warc_filename, warc_record_offset,
                            warc_record_length
                            FROM ccindex
                            WHERE crawl = 'CC-MAIN-2021-43'
                            AND subset = 'warc'
                      ''')
    sqldf = sqldf.filter(col('url_host_name').isin(list(DOMAINS.keys())))
    print("Got rows", sqldf.take(10))
    print("Num rows", sqldf.count())
    sqldf = sqldf.sample(fraction=0.001)
    warc_recs = sqldf.select("url", "warc_filename", "warc_record_offset", "warc_record_length").rdd
    rdd = warc_recs.mapPartitions(fetch_process_warc_records)
    output = sqlc.createDataFrame(rdd, schema=output_schema)
    output.write.option('compression', 'gzip').format('json').mode('overwrite').save(OUTPUT_PATH)


def fetch_process_warc_records(rows):
    """Fetch all WARC records defined by filenames and offsets in rows,
    parse the records and the contained HTML, split the text into words
    and emit pairs <word, 1>"""
    s3client = boto3.client('s3')
    for row in rows:
        warc_path = row['warc_filename']
        offset = int(row['warc_record_offset'])
        length = int(row['warc_record_length'])
        rangereq = 'bytes={}-{}'.format(offset, (offset+length-1))
        response = s3client.get_object(Bucket='commoncrawl',
        Key=warc_path,
        Range=rangereq)
        record_stream = BytesIO(response["Body"].read())
        for record in ArchiveIterator(record_stream):
            for result in process_record(record):
                yield result


def get_domain_rating(url):
    domain = urlparse(url).netloc
    return DOMAINS.get(domain)


def is_html(record):
    """Return true if (detected) MIME type of a record is HTML"""
    html_types = ['text/html', 'application/xhtml+xml']
    if (('WARC-Identified-Payload-Type' in record.rec_headers) and
        (record.rec_headers['WARC-Identified-Payload-Type'] in
         html_types)):
        return True
    content_type = record.http_headers.get_header('content-type', None)
    if content_type:
        for html_type in html_types:
            if html_type in content_type:
                return True
    return False


def justext(html_text, stoplist, length_low=LENGTH_LOW_DEFAULT,
            length_high=LENGTH_HIGH_DEFAULT, stopwords_low=STOPWORDS_LOW_DEFAULT,
            stopwords_high=STOPWORDS_HIGH_DEFAULT, max_link_density=MAX_LINK_DENSITY_DEFAULT,
            max_heading_distance=MAX_HEADING_DISTANCE_DEFAULT, no_headings=NO_HEADINGS_DEFAULT,
            encoding=None, default_encoding=DEFAULT_ENCODING,
            enc_errors=DEFAULT_ENC_ERRORS, preprocessor=preprocessor):
    """
    Converts an HTML page into a list of classified paragraphs. Each paragraph
    is represented as instance of class ˙˙justext.paragraph.Paragraph˙˙.
    """
    dom = html_to_dom(html_text, default_encoding, encoding, enc_errors)
    print("Parsed HTML")

    try:
        title = dom.find(".//title").text
    except AttributeError:
        title = None

    preprocessed_dom = preprocessor(dom)

    paragraphs = ParagraphMaker.make_paragraphs(preprocessed_dom)
    print("Got paragraphs")

    classify_paragraphs(paragraphs, stoplist, length_low, length_high,
                        stopwords_low, stopwords_high, max_link_density, no_headings)
    revise_paragraph_classification(paragraphs, max_heading_distance)

    return paragraphs, title


output_schema = StructType([
    StructField("uri", StringType(), False),
    StructField("title", StringType(), False),
    StructField("extract", StringType(), False),
])


def process_record(record):
    # print("Record", record.format, record.rec_type, record.rec_headers, record.raw_stream,
    #       record.http_headers, record.content_type, record.length)

    if record.rec_type != 'response':
        # skip over WARC request or metadata records
        return
    if not is_html(record):
        return

    uri = record.rec_headers.get_header('WARC-Target-URI')
    if len(uri) > MAX_URI_LENGTH:
        print("URI too long", len(uri))
        return

    # rating = get_domain_rating(uri)
    # print("Rating", rating)
    # if rating is None:
    #     return

    content = record.content_stream().read().strip()
    # print("Content", uri, content[:100])

    if not content:
        return

    try:
        all_paragraphs, full_title = justext(content, get_stoplist('English'))
    except UnicodeDecodeError:
        print("Unable to decode unicode")
        return
    except ParserError:
        print("Unable to parse")
        return

    if full_title is None:
        print("Missing title")
        return

    title = full_title[:NUM_TITLE_CHARS] + '…' \
        if len(full_title) > NUM_TITLE_CHARS else full_title

    text = '\n'.join([p.text for p in all_paragraphs
                      if not p.is_boilerplate])[:NUM_CHARS_TO_ANALYSE]
    print("Paragraphs", text)

    if len(text) < NUM_EXTRACT_CHARS:
        return

    language = detect(text)
    print("Got language", language)
    if language != 'en':
        return

    extract = text[:NUM_EXTRACT_CHARS]
    yield uri, title, extract


if __name__ == '__main__':
    run()
