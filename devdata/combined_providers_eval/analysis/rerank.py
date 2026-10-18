import json, numpy as np
from sklearn.metrics import ndcg_score
N=10
def gold_ndcg(urls,gold):
    y=[gold.get(u,0.0) for u in urls]+[0.0]*(N-len(urls))
    return ndcg_score([y],[list(range(N,0,-1))])
d=json.load(open('devdata/combined_providers_eval/rows-0.05.json'))
res={k:[] for k in ['staan','combined','minilm_top10','blend_top10','minilm_union','brave']}
for r in d:
    j,g,L=r['judged'],r['gold'],r['lists']
    c=L['combined'][:N]
    res['staan'].append(gold_ndcg(L['staan'][:N],g)); res['brave'].append(gold_ndcg(L['brave'][:N],g))
    res['combined'].append(gold_ndcg(c,g))
    res['minilm_top10'].append(gold_ndcg(sorted(c,key=lambda u:-j[u]),g))
    # blend: minilm score minus a small position prior from LTR order
    res['blend_top10'].append(gold_ndcg(sorted(c,key=lambda u:-(j[u]-0.03*c.index(u))),g))
    pool=list(dict.fromkeys(c+L['staan'][:N]))
    res['minilm_union'].append(gold_ndcg(sorted(pool,key=lambda u:-j.get(u,0))[:N],g))
base=np.array(res['combined'])
rng=np.random.default_rng(0)
for k,v in res.items():
    v=np.array(v); diff=v-base
    bs=[diff[rng.integers(0,len(v),len(v))].mean() for _ in range(2000)]
    print(f'{k:14s} gold {v.mean():.3f}  vs combined {diff.mean():+.3f} [{np.percentile(bs,2.5):+.3f},{np.percentile(bs,97.5):+.3f}]')
print('---')
def jn(urls,j):
    ideal=sorted(j.values(),reverse=True)[:N]
    dcg=lambda xs: sum(x/np.log2(i+2) for i,x in enumerate(xs))
    return dcg([j[u] for u in urls])/dcg(ideal)
out={k:([],[]) for k in ['combined','staan','staan_fill','staan_fill_k1']}
for r in d:
    j,g,L=r['judged'],r['gold'],r['lists']
    c=L['combined'][:N]; s=L['staan'][:N]
    extra=[u for u in c if u not in s]
    fill=(s+extra)[:N]
    # one index result allowed at slot 3 if combined ranked it top 3
    top_idx=[u for u in c[:3] if u not in s][:1]
    k1=(s[:2]+top_idx+[u for u in s[2:] ]+[u for u in extra if u not in top_idx])[:N]
    for k,lst in [('combined',c),('staan',s),('staan_fill',fill),('staan_fill_k1',k1)]:
        out[k][0].append(gold_ndcg(lst,g)); out[k][1].append(jn(lst,j))
for k,(gg,jj) in out.items(): print(f'{k:14s} gold {np.mean(gg):.3f} judge {np.mean(jj):.3f}')
