"""
Create a search index
"""
from typing import Iterable
from urllib.parse import unquote

from mwmbl.tinysearchengine.indexer import TokenizedDocument
from mwmbl.tokenizer import tokenize, get_bigrams

DEFAULT_SCORE = 0

HTTP_START = 'http://'
HTTPS_START = 'https://'
NUM_FIRST_TOKENS = 10
NUM_BIGRAMS = 10


STOPWORDS = set("0,1,2,3,4,5,6,7,8,9,a,A,about,above,across,after,again,against,all,almost,alone,along,already,also," \
            "although,always,am,among,an,and,another,any,anyone,anything,anywhere,are,aren't,around,as,at,b,B,back," \
            "be,became,because,become,becomes,been,before,behind,being,below,between,both,but,by,c,C,can,cannot,can't," \
            "could,couldn't,d,D,did,didn't,do,does,doesn't,doing,done,don't,down,during,e,E,each,either,enough,even," \
            "ever,every,everyone,everything,everywhere,f,F,few,find,first,for,four,from,full,further,g,G,get,give,go," \
            "h,H,had,hadn't,has,hasn't,have,haven't,having,he,he'd,he'll,her,here,here's,hers,herself,he's,him," \
            "himself,his,how,however,how's,i,I,i'd,if,i'll,i'm,in,interest,into,is,isn't,it,it's,its,itself,i've," \
            "j,J,k,K,keep,l,L,last,least,less,let's,m,M,made,many,may,me,might,more,most,mostly,much,must,mustn't," \
            "my,myself,n,N,never,next,no,nobody,noone,nor,not,nothing,now,nowhere,o,O,of,off,often,on,once,one,only," \
            "or,other,others,ought,our,ours,ourselves,out,over,own,p,P,part,per,perhaps,put,q,Q,r,R,rather,s,S,same," \
            "see,seem,seemed,seeming,seems,several,shan't,she,she'd,she'll,she's,should,shouldn't,show,side,since,so," \
            "some,someone,something,somewhere,still,such,t,T,take,than,that,that's,the,their,theirs,them,themselves," \
            "then,there,therefore,there's,these,they,they'd,they'll,they're,they've,this,those,though,three,through," \
            "thus,to,together,too,toward,two,u,U,under,until,up,upon,us,v,V,very,w,W,was,wasn't,we,we'd,we'll,well," \
            "we're,were,weren't,we've,what,what's,when,when's,where,where's,whether,which,while,who,whole,whom,who's," \
            "whose,why,why's,will,with,within,without,won't,would,wouldn't,x,X,y,Y,yet,you,you'd,you'll,your,you're," \
            "yours,yourself,yourselves,you've,z,Z".split(','))


def prepare_url_for_tokenizing(url: str):
    if url.startswith(HTTP_START):
        url = url[len(HTTP_START):]
    elif url.startswith(HTTPS_START):
        url = url[len(HTTPS_START):]
    for c in '/._':
        if c in url:
            url = url.replace(c, ' ')
    return url


def get_index_tokens(tokens):
    first_tokens = tokens[:NUM_FIRST_TOKENS]
    bigrams = get_bigrams(NUM_BIGRAMS, tokens)
    return set(first_tokens + bigrams)


def tokenize_document(url, title_cleaned, extract, score):
    title_tokens = tokenize(title_cleaned)
    prepared_url = prepare_url_for_tokenizing(unquote(url))
    url_tokens = tokenize(prepared_url)
    extract_tokens = tokenize(extract)
    # print("Extract tokens", extract_tokens)
    tokens = get_index_tokens(title_tokens) | get_index_tokens(url_tokens) | get_index_tokens(extract_tokens)
    # doc = Document(title_cleaned, url, extract, score)
    # token_scores = {token: score_result([token], doc, True) for token in tokens}
    # high_scoring_tokens = [k for k, v in token_scores.items() if v > 0.5]
    # print("High scoring", len(high_scoring_tokens), token_scores, doc)
    document = TokenizedDocument(tokens=list(tokens), url=url, title=title_cleaned, extract=extract, score=score)
    return document
