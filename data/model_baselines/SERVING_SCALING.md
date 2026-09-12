# llama.cpp serving scaling — 2026-09-12

Measured to find where citadel's serving throughput goes and what is portable from vLLM without a
kernel rewrite. **Conclusion: there is no scheduler gap. llama-server already delivers the kernel
ceiling.** The remaining lever is concurrency depth, which fixed per-slot KV prevents.

Hardware: RTX 5090 Laptop, 23.46 GiB usable, sm_120 (Blackwell).
Build: `llama.cpp` branch `vllm-equivalence` off master, `-DCMAKE_CUDA_ARCHITECTURES=120a`.
Model: `Qwen3.8-27B-UD-IQ3_XXS.gguf` (10.9 GB), f16 KV.

## Kernel ceiling — `llama-batched-bench`

Short context (`-npp 2048 -ntg 256 -c 49152`):

| B | S_PP t/s | S_TG t/s | vs B=1 |
| --- | --- | --- | --- |
| 1 | 1491.15 | 45.29 | 1.00× |
| 2 | 1460.38 | 79.39 | 1.75× |
| 4 | 1451.92 | 114.82 | 2.54× |
| 8 | 1440.54 | 151.58 | 3.35× |
| 16 | 1419.76 | 241.20 | **5.33×** |

Long context, matching real request shape (`-npp 4700 -ntg 600 -c 90112`):

| B | S_PP t/s | S_TG t/s | per slot | vs B=1 |
| --- | --- | --- | --- | --- |
| 1 | 1387.70 | 42.18 | 42.18 | 1.00× |
| 2 | 1364.41 | 73.83 | 36.92 | 1.75× |
| 4 | 1346.57 | 105.82 | 26.46 | 2.51× |
| 8 | 1320.13 | 136.81 | 17.10 | 3.24× |
| 16 | 1278.85 | 214.49 | 13.41 | **5.09×** |

Decode scales ~5× from B=1 to B=16 at both context shapes, with no flattening. **The batched kernels
are not the ceiling.** The "flat beyond 4 slots" figure in `EXCEL_STATE.md` was a server-level
measurement and does not describe the kernels.

Prefill is flat in B and grows linearly in wall time — batching does nothing for it.

## The server is already at that ceiling

16-document battery, `--parallel 4 -ub 512`, from `/metrics` and `llama-batched-bench`:

```text
prefill:  71,588 tok ÷   599 tok/s (measured, mmproj loaded)  = 119.5 s
decode :   9,243 tok ÷ 105.8 tok/s (batched-bench B=4)        =  87.4 s
                                                    predicted = 206.9 s
                                                       actual = 206.7 s
```

**0.1% off.** No scheduler gap exists.

An earlier version of this file claimed ~1.9× was "lost inside the server." That was wrong. It came
from summing `llamacpp:tokens_predicted_seconds_total` (639.9s), which is per-slot wall time
*including time spent waiting on other slots*, and comparing it against batched-bench's clean phase
timings. The uncontaminated counters are `n_decode_total` (2,839 steps) and
`n_busy_slots_per_decode` (3.28) — 2,839 × 3.28 = 9,243 tokens, exactly on batched-bench's curve at
B ≈ 3.3.

## Hypotheses tested and rejected

| hypothesis | verdict |
| --- | --- |
| q8_0 KV excludes the batched MMA attention kernel | **false.** `fattn.cu` routes quantized KV to `BEST_FATTN_KERNEL_MMA_F16` for `Q->ne[1] > 2`; the `f16` in the name is the compute type |
| CUDA graph churn costs ~2× | **false.** Measured 86-90% reuse (see instrumentation below) |
| big prefill ubatches stall decode | **false.** `-ub 128` made prefill *and* decode *and* reuse all worse — see below |
| the server loses ~1.9× to scheduling | **false.** Predicted wall matched actual to 0.1% |

### `-ub 512` vs `-ub 128`

| | `-ub 512` | `-ub 128` |
| --- | --- | --- |
| wall | **206.7s** | 226.6s |
| prefill | 444 tok/s | 387 tok/s |
| decode per slot | 15.68 | 14.44 |
| graph reuse | 86-90% | 82-85% |

Smaller ubatches cost more in per-kernel overhead than they save in stalls, and they add prefill
shape variety which lowers graph reuse. Do not lower `-ub`.

## Where the time actually goes

| | tokens | rate | time | share |
| --- | --- | --- | --- | --- |
| **prefill** | 71,588 | 599 tok/s | 119.5s | **58%** |
| decode | 9,243 | 105.8 tok/s | 87.4s | 42% |

Input:output is **7.8:1** on the OCR battery, so this particular workload is prefill-dominated and
batched-bench's decode curve governs the smaller half. A conversational workload inverts this.

**The vision encoder costs 2.25× on prefill:** 599 tok/s with mmproj loaded against 1,346 tok/s
without, measured on the same model and context shape.

**The prompt cache is getting zero hits:** `llamacpp:prompt_tokens_cached_total 0` across 16
requests that share an identical 646-token prompt. Cause is content ordering in `call_page_ocr` —
the image occupies position 0, so the common prefix is length 0 and the shared prompt sits behind it
where it can never be cached. Reordering is one line but is not quality-neutral: Qwen-VL is trained
image-then-text.

## Instrumentation added

`vllm-equivalence` adds `n_graph_computes` — the denominator for `n_reused`, which upstream reports
without one:

- `include/llama.h` — field in `llama_perf_context_data`
- `src/llama-context.h` — counter member
- `src/llama-context.cpp` — `n_graph_computes++` in `process_ubatch` before the reuse check; exposed
  in `perf_get_data`, cleared in `perf_reset`, printed with a percentage
- `tools/server/server-context.cpp` — `graphs reused = N / M computes (X %)`

## Porting scope

### Paged KV (PR #22569)

48 files, ~4,029 insertions. Per-model changes are mechanical — one line each,
`build_attn_inp_kv()` → `build_attn_inp_kv_auto()` — across 11 dense models: command-r, falcon,
gemma, gemma3, internlm2, llama, mistral3, phi3, qwen2, qwen3, starcoder2.

**It does not cover `qwen35`.** `src/models/qwen35.cpp:149` builds the main graph with
`build_inp_mem_hybrid()`, because 16 of 64 layers are full attention and the other 48 are Gated
DeltaNet with a recurrent state. The PR never touches the hybrid builder.

So the work splits:

1. Rebase #22569 onto master. Gives paging for the dense models, not the served one.
2. Give `build_inp_mem_hybrid` a paged variant — page the KV for the 16 attention layers, leave the
   recurrent state per-sequence since it is fixed-size and does not want paging. Integrating
   `llama-kv-cache-paged` with `llama_memory_hybrid`. Nobody upstream has done this.

### MTP (PR #22673, merged to master)

**Implemented for `qwen35`.** `src/models/qwen35.cpp:538` builds the nextn draft head
(`mtp_h_input`, `mtp_hnorm`, `layer.nextn.hnorm`). The `unused tensor blk.64.nextn.*` warnings
appear only because that graph is built on demand, under `--spec-type draft-mtp`.

Restriction: **`--parallel 1` only.** Known issues include *"recurrent state save/load with partial
rollback incomplete"* — the same per-sequence state surgery the hybrid paging work needs, which makes
an MTP test the cheapest available probe of that capability.

### Mutually exclusive

MTP needs `--parallel 1`; paging exists to serve `--parallel N`. At parallel 1, MTP projects to
~42 × 1.7 ≈ 71 tok/s single-stream. Batching already gives 105.8 aggregate at B=4 and 214.5 at B=16.
MTP wins for one interactive user; batching wins for throughput. One server instance cannot do both,
and citadel needs conversational *and* batch — so this is an architectural fork to decide before
either is built.

## Chat template — froggeric/Qwen-Fixed-Chat-Templates v22.5

Qwen3.8-27B is a **hybrid** thinking model — one checkpoint, mode selected at inference time — so the
mode switch is implemented in the chat template, which makes template correctness load-bearing. There is
no separate `-Instruct` GGUF to download; `enable_thinking: false` (already set in every payload builder)
*is* instruct mode.

The repo independently reproduces a bug already recorded in `EXCEL_STATE.md`: *"`xhigh` + JSON schema
returns empty content on 8 of 8 sheets, on two servers"* appears there as "default `xhigh` burned budgets
with zero content returned." That match is why it was worth testing.

**Static validation passed** — every Jinja feature the 28 KB template uses is implemented by llama.cpp's
in-house engine (`common/jinja/`, PR#18462, not minja):

- filters `join`, `length`, `lower`, `string`, `tojson`, `trim`
- functions `namespace()`, `raise_exception`
- string methods `.split()`, `.startswith()`, `.endswith()`, `.items()`
- tests `is defined`, `is undefined`, `is none`, `is iterable`, `is mapping`, `is string`
  (all registered as `test_is_*` in `value.cpp`)

It loads without a parse error and llama.cpp still detects reasoning-preserve support in it.

**Measured effect**, `scripts/collapse_test.py` over 18 regions, `call_structured` + JSON schema,
stock template vs fixed, everything else identical:

```diff
  sheet1_region9_r4570-5396_c2-15
- [0] box=(0,826,0,13) headers=[1, 2]    data=4..825 meta=[] title_row=0
+ [0] box=(0,826,0,13) headers=[1, 2, 3] data=4..825 meta=[] title_row=0
```

**One difference in 18 regions, and it is a fix.** `headers=[1,2,3]` is exactly the value
`TABULAR_STATE.md` validated on Q4_K_M across four consecutive runs; IQ3 with the stock template was
dropping the third header row of region9's 3-level header. 17 of 18 byte-identical, so no regressions.

Adopted in `compose.yaml` as `--chat-template-file /models/chat_template.jinja` (the file lives in
`data/gguf_models/`, which is already mounted at `/models`).

Caveats:

- One data point on one token. The plausible mechanism is the repo's "empty think poisoning" fix — the
  official template injecting a blank `<think></think>` under `enable_thinking: false` — but that is
  inferred, not demonstrated.
- The fixes aimed at multi-turn KV invalidation, tool calls and reasoning extraction are **untested**
  here; citadel has no multi-turn harness. They matter for the conversational instance, not this path.
- `scripts/qwen-no-think.jinja` (7.7 KB) is an earlier hand-made override and is **unreferenced** by
  anything. Dead file.

### Still broken in both

`region9` returns `meta=[]` where `TABULAR_STATE.md` expects `meta=[826]`. Row 826 — the Grand Total —
sits inside `box=(0,826,…)` but in neither `data=4..825` nor `meta`, so it is orphaned entirely. Not a
template issue.

Note `scripts/collapse_test.py` tests the **superseded** design: its schema field is `tables`, whereas
`EXCEL_STATE.md` records that the field must be `blocks` ("asking for 'tables' made the model drop a
non-table block"), and its inputs are pre-rendered region dumps rather than the sheet windows the current
stage-1 probe uses. It remains valid as a *differential* probe of the JSON-schema path, which is all it
was used for here.

## Not portable

- **Marlin-class W4A16 GEMM** — incompatible with codebook i-quants; IQ3_XXS unpacks to Q8_0 tiles
  (`MMQ_DP4A_TXS_Q8_0`). The route is NVFP4 weights, which already have an MMQ instance and native
  Blackwell FP4 tensor cores. A quant choice, not a kernel project.
- **FP8 KV** — not a ggml type. `ggml_cuda_fattn_kv_type_supported` accepts only
  F32/F16/BF16/Q4_0/Q4_1/Q5_0/Q5_1/Q8_0, and `ggml.h:92` says FP8 support is theoretical.
  `IQ4_NL` is settable via `--cache-type-*` but has **no** flash-attention support — avoid it.
