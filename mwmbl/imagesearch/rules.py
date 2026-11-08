"""Deterministic checks that run before any model.

They are deliberately blunt: a word that is sometimes innocent (a chicken breast, a Roman
bath) still excludes the image, because the cost of a wrong exclusion is a missing picture and
the cost of a wrong inclusion is not. The list is of sexual and nudity terms; it is a first line
the models back up, not a substitute for them.
"""

import re

from mwmbl.imagesearch.types import SourceImage, Verdict

DENIED_TERMS = [
    r"nude\w*",
    r"nudity",
    r"nudist\w*",
    r"naturist\w*",
    r"naked",
    r"topless",
    r"bottomless",
    r"undress\w*",
    r"strip(?:per|pers|tease|club)\w*",
    r"sex(?!t)\w*",
    r"erotic\w*",
    r"porn\w*",
    r"nsfw",
    r"xxx",
    r"hentai",
    r"fetish\w*",
    r"bdsm",
    r"bondage",
    r"genital\w*",
    r"penis\w*",
    r"phall\w*",
    r"vagina\w*",
    r"vulva\w*",
    r"clitor\w*",
    r"anus",
    r"anal",
    r"testicle\w*",
    r"scrotum",
    r"breast\w*",
    r"boob\w*",
    r"nipple\w*",
    r"buttock\w*",
    r"pubic",
    r"lingerie",
    r"underwear",
    r"panties",
    r"thong",
    r"upskirt\w*",
    r"voyeur\w*",
    r"masturbat\w*",
    r"orgasm\w*",
    r"ejaculat\w*",
    r"semen",
    r"fellatio",
    r"cunnilingus",
    r"intercourse",
    r"prostitut\w*",
    r"escort\w*",
    r"incest\w*",
    r"rape\w*",
    r"p(?:a)?edophil\w*",
    r"child\s*abuse",
    r"lolita",
]
DENIED_PATTERN = re.compile(r"\b(?:" + "|".join(DENIED_TERMS) + r")\b", re.IGNORECASE)


def find_denied_term(text: str) -> str | None:
    match = DENIED_PATTERN.search(text)
    return match.group(0) if match else None


def query_verdict(query: str) -> Verdict:
    return Verdict.NSFW if find_denied_term(query) else Verdict.SAFE


def image_verdict(image: SourceImage) -> Verdict:
    return Verdict.NSFW if find_denied_term(image.text) else Verdict.SAFE
