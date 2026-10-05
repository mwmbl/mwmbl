import time, numpy as np, onnxruntime as ort
from tokenizers import Tokenizer
D='devdata/judge_train/models/minilm-both-v1/onnx/'
tok=Tokenizer.from_file(D+'tokenizer.json')
for threads in (1,2,4):
  so=ort.SessionOptions(); so.intra_op_num_threads=threads
  s=ort.InferenceSession(D+'model.onnx',so)
  names=[i.name for i in s.get_inputs()]
  q="how to fix a leaking kitchen tap"
  doc="How to Fix a Leaky Faucet | Step-by-step guide - "+"Replacing the washer or cartridge is usually all it takes to stop a dripping tap. "*4
  for n,L in ((15,80),(20,80)):
    tok.enable_truncation(L); tok.enable_padding(length=L)
    enc=tok.encode_batch([(q,doc)]*n)
    feed={'input_ids':np.array([e.ids for e in enc],dtype=np.int64),'attention_mask':np.array([e.attention_mask for e in enc],dtype=np.int64),'token_type_ids':np.array([e.type_ids for e in enc],dtype=np.int64)}
    feed={k:v for k,v in feed.items() if k in names}
    s.run(None,feed)
    ts=[]
    for _ in range(10):
      t=time.perf_counter(); s.run(None,feed); ts.append(time.perf_counter()-t)
    print(f'threads={threads} n={n} len={L}: median {np.median(ts)*1000:.0f} ms')
