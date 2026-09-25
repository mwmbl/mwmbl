import json, math, numpy as np
d=json.load(open('devdata/combined_providers_eval/rows-0.05-handover.json'))
print(list(d[0]['lists'].keys()))
def ndcg(lst, judged):
    g=[judged.get(u,0) for u in lst[:10]]
    ideal=sorted(judged.values(),reverse=True)[:10]
    dcg=lambda xs: sum(x/math.log2(i+2) for i,x in enumerate(xs))
    return dcg(g)/dcg(ideal) if dcg(ideal)>0 else 0
comb='combined'
rows=[]
for r in d:
    j=r['judged']; L=r['lists']
    m=L['mwmbl']; c=L[comb]
    top=[j.get(u,0) for u in m[:3]]
    rows.append((ndcg(m,j), ndcg(c,j), max(top) if top else 0, np.mean(top) if top else 0, len(m)))
a=np.array(rows)
print('mean mwmbl',a[:,0].mean(),'comb',a[:,1].mean())
print('oracle: frac mwmbl>=comb-0.01', (a[:,0]>=a[:,1]-0.01).mean())
for t in [0.6,0.7,0.8,0.9]:
    s=a[:,2]>=t
    loss=(a[s,1]-a[s,0]).sum()/len(a)
    print(f'gate max top3 judge>={t}: skip {s.mean():.2%}, ndcg loss overall {loss:.3f}, loss on skipped {(a[s,1]-a[s,0]).mean() if s.any() else 0:.3f}')
