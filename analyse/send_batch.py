"""
Send a batch to a running instance.
"""
import requests

from mwmbl.crawler.batch import Batch, Item, ItemContent


URL = 'http://localhost:5000/crawler/batches/'


def run():
    batch = Batch(user_id='test_user_id111111111111111111111111', items=[Item(
        url='https://www.theguardian.com/stage/2007/nov/18/theatre',
        content=ItemContent(
            title='A nation in search of the new black | Theatre | The Guardian',
            extract="Topic-stuffed and talk-filled, Kwame Kwei-Armah's new play proves that issue-driven drama is (despite reports of its death) still being written and staged…",
            links=[]),
        timestamp=123456,
        status=200,
    )])
    result = requests.post(URL, data=batch.json())
    print("Result", result.content)


if __name__ == '__main__':
    run()
