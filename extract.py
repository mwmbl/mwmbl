"""
Extract content from HTML files and store it as compressed JSON
"""

from urllib.parse import urlparse

from pyspark.sql import SparkSession
from pyspark.sql.functions import col
from pyspark.sql.types import StructType, StructField, StringType, LongType, IntegerType

RECORDS_PATH = 's3://tinysearch/outputs/records'
OUTPUT_PATH = 's3://tinysearch/outputs/index'

NUM_PAGES = 1024
MAX_RESULTS_PER_HASH = 200
PAGE_SIZE = 4096


index_schema = StructType([
    StructField("term_hash", LongType(), False),
    StructField("data", StringType(), False),
    StructField("top", StringType(), False),
])


output_schema = StructType([
    StructField("uri", StringType(), False),
    StructField("title", StringType(), False),
    StructField("extract", StringType(), False),
])


record_schema = StructType([
    StructField("url", StringType(), False),
    StructField("warc_filename", StringType(), False),
    StructField("warc_record_offset", IntegerType(), False),
    StructField("warc_record_length", IntegerType(), False),
])


spark = SparkSession \
    .builder \
    .appName("Python Spark SQL basic example") \
    .config("spark.some.config.option", "some-value") \
    .getOrCreate()


def run():
    # sqlc = SQLContext(sparkContext=spark)

    df = spark.read.load('s3://commoncrawl/cc-index/table/cc-main/warc/')
    df.createOrReplaceTempView('ccindex')
    sqldf = spark.sql('''SELECT url, warc_filename, warc_record_offset,
                            warc_record_length
                            FROM ccindex
                            WHERE crawl = 'CC-MAIN-2021-43'
                            AND subset = 'warc'
                      ''')
    sqldf = sqldf.sample(fraction=0.01)
    sqldf = sqldf.filter(col('url_host_name').isin(list(DOMAINS.keys())))
    # print("Got rows", sqldf.take(10))
    # print("Num rows", sqldf.count())
    sqldf.write.option('compression', 'gzip').format('json').mode('overwrite').save(RECORDS_PATH)

    # warc_recs = sqldf.select("url", "warc_filename", "warc_record_offset", "warc_record_length").rdd
    # rdd = warc_recs.mapPartitions(fetch_process_warc_records)
    # output = sqlc.createDataFrame(rdd, schema=output_schema)
    # output.write.option('compression', 'gzip').format('json').mode('overwrite').save(OUTPUT_PATH)


def get_domain_rating(url):
    domain = urlparse(url).netloc
    return DOMAINS.get(domain)


if __name__ == '__main__':
    run()
