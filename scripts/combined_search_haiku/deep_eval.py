"""MiniLM re-ranks of the combined LTR top N, with and without Wikipedia, graded by UK Haiku."""

import glob
import json
import os
import random
import sys

import numpy as np

S = os.environ.get("HAIKU_WORK_DIR", "devdata/combined_providers_eval/haiku_work")
P = "devdata/combined_providers_eval/engb/"
N = 10
rows = json.load(open(P + "rows-0.05-with-brave.json"))
deep = json.load(open(P + "combined_top30.json"))
wiki = json.load(open(P + "wiki_pool.json"))
text = json.load(open(P + "pool_text.json"))


def load_grades():
    G = {}
    for d in ("haiku_uk", "haiku_uk_deep"):
        try:
            man = {int(k): v for k, v in json.load(open(f"{S}/{d}/manifest.json")).items()}
        except FileNotFoundError:
            continue
        qmap = (
            {int(k): v for k, v in json.load(open(f"{S}/{d}/queries.json")).items()}
            if d != "haiku_uk"
            else {i: r["query"] for i, r in enumerate(rows)}
        )
        for f in glob.glob(f"{S}/{d}/out_*.jsonl"):
            for line in open(f):
                line = line.strip()
                if not line.startswith("{"):
                    continue
                o = json.loads(line)
                urls = man[o["qid"]]
                g = o["grades"]
                if isinstance(g, dict) and sorted(map(int, g)) == list(range(len(urls))):
                    G.setdefault(qmap[o["qid"]], {}).update({urls[int(k)]: v for k, v in g.items()})
    return G


def arms(r):
    q = r["query"]
    s = r["lists"]["staan"][:N]
    c = r["lists"]["combined"][:N]
    D = deep[q]
    W = wiki[q]
    score = {d["url"]: d["minilm"] for d in D + W}

    def mini(cands):
        return sorted(dict.fromkeys(cands), key=lambda u: -score.get(u, 0))[:N]

    a = {"combined (shipped)": c, "staan": s, "staan-first + fill": (s + [u for u in c if u not in s])[:N]}
    for k in (10, 20, 30):
        top = [d["url"] for d in D[:k]]
        a[f"minilm top{k}"] = mini(top)
        a[f"minilm top{k} + wiki"] = mini(top + [w["url"] for w in W])
    rest = mini([d["url"] for d in D] + [w["url"] for w in W])
    a["staan-first, fill minilm(top30+wiki)"] = (s + [u for u in rest if u not in s])[:N]
    a["brave"] = r["lists"]["brave"][:N]
    return a


def doc(q, u):
    for d in deep[q] + wiki[q]:
        if d["url"] == u:
            return d["title"], d["extract"]
    t = text[q].get(u)
    return (t[0], t[1]) if t else None


if sys.argv[1] == "dump":
    G = load_grades()
    todo = {}
    for r in rows:
        q = r["query"]
        g = G.get(q, {})
        need = {u for urls in arms(r).values() for u in urls} - set(g)
        if need:
            todo[q] = sorted(need)
    print("queries needing grades", len(todo), "urls", sum(map(len, todo.values())))
    exec(
        open(f"{S}/make_batches_uk.py").read().split("PROMPT = ")[1].split("manifest = {}")[0].join(["PROMPT = ", ""])
        if False
        else ""
    )
    prompt = open(f"{S}/haiku_uk/batch_00.txt").read().split("=== QUERIES ===")[0]
    import os

    os.makedirs(f"{S}/haiku_uk_deep", exist_ok=True)
    man = {}
    items = list(todo.items())
    NB = int(sys.argv[2])
    for qid, (q, urls) in enumerate(items):
        urls = urls[:]
        random.Random(qid).shuffle(urls)
        man[qid] = (q, urls)
    json.dump({k: v[1] for k, v in man.items()}, open(f"{S}/haiku_uk_deep/manifest.json", "w"))
    json.dump({k: v[0] for k, v in man.items()}, open(f"{S}/haiku_uk_deep/queries.json", "w"))
    for b in range(NB):
        parts = [prompt, "=== QUERIES ===\n"]
        for qid in list(man)[b::NB]:
            q, urls = man[qid]
            parts.append(f"\n## qid {qid} | query: {q}\n")
            for i, u in enumerate(urls):
                t, e = doc(q, u)
                parts.append(f"[{i}] {t[:150]}\n    {u[:200]}\n    {(e or '').replace(chr(10), ' ')[:300]}\n")
        open(f"{S}/haiku_uk_deep/batch_{b:02d}.txt", "w").write("".join(parts))

if sys.argv[1] == "score":
    G = load_grades()

    def dcg(g):
        return sum((2**x - 1) / np.log2(i + 2) for i, x in enumerate(g))

    res = {}
    used = 0
    for r in rows:
        q = r["query"]
        g = G.get(q, {})
        A = arms(r)
        if any(u not in g for urls in A.values() for u in urls):
            continue
        used += 1
        ideal = dcg(sorted(g.values(), reverse=True)[:N])
        for k, urls in A.items():
            res.setdefault(k, []).append(dcg([g[u] for u in urls]) / ideal if ideal else 0)
    rng = np.random.default_rng(0)
    base = np.array(res["staan-first + fill"])
    br = np.array(res["brave"])

    def ci(d):
        bs = [d[rng.integers(0, len(d), len(d))].mean() for _ in range(4000)]
        return f"{d.mean():+.3f} [{np.percentile(bs, 2.5):+.3f},{np.percentile(bs, 97.5):+.3f}]"

    print(f"fully graded queries: {used}/{len(rows)}")
    print(f"{'arm':38s} haiku   vs staan-first             vs brave")
    for k, v in res.items():
        v = np.array(v)
        print(f"{k:38s} {v.mean():.3f}   {ci(v - base)}   {ci(v - br)}")
