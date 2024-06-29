import sqlite3


def find_matching_urls(url_db_path: str, urls: list[str]) -> list[str]:
    if not urls:
        return []

    connection = sqlite3.connect(url_db_path)
    seq = ','.join(['?'] * len(urls))
    result = connection.execute(f"""
        SELECT url from urls
        WHERE url IN ({seq})
    """, urls)
    return [row[0] for row in result.fetchall()]
