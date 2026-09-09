# 03 — Research landscape: proactive & ambient agents (evidence base)

Research agent report, 2026-08-25. Target platform assumed throughout:
single-process FastAPI + asyncio, Postgres-only persistence,
LangChain/LangGraph loops, tools/skills/sub-agents registry, HITL gates, runs
ledger, episodic/semantic/procedural memory with idle-triggered consolidation
(already informed by sleep-time compute, [arXiv:2504.13171](https://arxiv.org/abs/2504.13171)).

---

## 1. When should an agent initiate? Proactive dialogue, mixed-initiative, and proactive-agent benchmarks

**Classic foundation.** The canonical answer is Horvitz's *Principles of Mixed-Initiative User Interfaces* (CHI '99, [DOI 10.1145/302979.303030](https://doi.org/10.1145/302979.303030), [PDF](https://erichorvitz.com/chi99horvitz.pdf)): twelve principles, of which the load-bearing ones for ambient mode are (a) act autonomously only when the *expected utility of action exceeds inaction* given uncertainty about the user's goals, (b) explicitly model the *status of the user's attention* in the timing of services, (c) provide dialog to resolve uncertainty rather than guessing, (d) minimize the cost of poor guesses about action and timing, and (e) maintain a memory of recent interactions to inform future initiative. The companion *Attention-Sensitive Alerting* work (UAI '99, [arXiv:1301.6707](https://arxiv.org/abs/1301.6707)) operationalized this as a decision-theoretic balance between the context-sensitive cost of deferral and the cost of interruption — i.e., initiative is a continuous expected-value calculation, not a rule. Anticipatory computing formalized the sensing side: Pejovic & Musolesi's survey (*ACM Computing Surveys* 47(3), [DOI 10.1145/2693843](https://doi.org/10.1145/2693843), [arXiv:1306.2356](https://arxiv.org/abs/1306.2356)) frames anticipation as context sensing → prediction → intelligent-action timing.

**Proactive dialogue systems.** Deng et al.'s IJCAI '23 survey ([arXiv:2305.02750](https://arxiv.org/abs/2305.02750)) organizes proactivity as target-guided dialogue, uncertainty-driven clarification, and non-collaborative handling; its follow-up, *Proactive Conversational AI: A Comprehensive Survey* ([ACM version](https://ink.library.smu.edu.sg/sis_research/10225/)), extends this to LLM agents. The consistent definition across this literature: proactivity = anticipation of needs + initiation without explicit command + goal-directedness.

**LLM proactive-agent benchmarks (2024–2026), with numbers:**

- **ProactiveBench** (*Proactive Agent*, [arXiv:2410.12361](https://arxiv.org/abs/2410.12361)): 6,790 events from real activity traces; a reward model trained on human accept/reject labels serves as automatic judge. A fine-tuned open model reached **66.47% F1** at deciding when to offer assistance, beating all tested open and closed models — i.e., off-the-shelf frontier models are *below two-thirds F1* at the fire/hold decision.
- **PROBE** (*Beyond Reactivity*, [arXiv:2510.19771](https://arxiv.org/abs/2510.19771)): decomposes proactivity into search-for-unspecified-issues → identify-bottleneck → resolve. Best end-to-end success: **40%** (GPT-5 and Claude Opus 4.1) — proactive problem-finding remains a frontier weakness.
- **ProactiveEval** ([arXiv:2508.20973](https://arxiv.org/abs/2508.20973)): 328 environments across 6 domains, 22 models; decomposes proactivity into *target planning* vs *dialogue guidance* and finds no single model wins both (DeepSeek-R1 best at planning, Claude-3.7-Sonnet best at guidance).
- **ProAct / idle-time proactive agents** (*Anticipate and Learn*, [arXiv:2605.25971](https://arxiv.org/abs/2605.25971)): agents that use idle time between turns to predict upcoming needs and pre-gather evidence cut conversation turns **14.8%**, user effort **11.7%**, and hallucinations **28.1%** across 200 scenarios / 40 domains. **PRIME** ([arXiv:2604.07645](https://arxiv.org/abs/2604.07645)) achieves similar proactive-reasoning gains training-free by evolving a structured experience memory (successful strategies / failure patterns / user preferences) — directly analogous to a procedural-memory layer.

**HCI studies of LLM proactivity (what users actually tolerate):**

- *Need Help? Designing Proactive AI Assistants for Programming* (CHI '25, [DOI 10.1145/3706598.3714002](https://doi.org/10.1145/3706598.3714002), [arXiv:2410.04596](https://arxiv.org/abs/2410.04596)): randomized study; proactive chat suggestions produced significant productivity benefit, **but** the "persistent suggest" condition (always-visible proactivity) was rated "distracting" and "annoying," while gated *Suggest*/*Suggest-and-Preview* conditions were viewed positively. Presentation and gating, not the proactivity itself, determined acceptance. Companion study *Assistance or Disruption?* (Codellaborator, CHI '25, [DOI 10.1145/3706598.3713357](https://doi.org/10.1145/3706598.3713357)) reaches the same conclusion from the other side: proactive initiative helps but must be scoped by context awareness and workspace presence cues.
- **LlamaPIE** ([arXiv:2505.04066](https://arxiv.org/abs/2505.04066), ACL Findings '25): proactive in-ear assistant using a **two-model pipeline — a small model decides *when* to respond, a large model decides *what*** — whispering 1–3 words and staying silent by default. Users preferred it over both no-assistance and a reactive assistant. *Proactive Conversational Agents with Inner Thoughts* (CHI '25, [DOI 10.1145/3706598.3713760](https://doi.org/10.1145/3706598.3713760)) similarly models a covert "thought" stream with an intrinsic-motivation threshold for speaking.
- **Do Proactive Agents Really Need an LLM to Decide When to Wake?** ([arXiv:2605.30152](https://arxiv.org/abs/2605.30152)): a temporal-graph model over native (actor, verb, object, timestamp) event tuples beats LLM-based wake decisions by **+16.7 F1 mean (max +46.0)** across 14 backbones, runs **12–83× faster** on consumer hardware at **~11 ms/event in ~220 MiB** — strong evidence that the *wake decision should not be an LLM call per event*.
- *Sensing What Surveys Miss* ([arXiv:2602.00880](https://arxiv.org/abs/2602.00880), N=32): cognitively-aligned intervention timing improved task accuracy **21%** and cut false negatives from **50.9% → 22.9%** vs misaligned timing — same content, different timing, large outcome difference.
- *Communication Policy Evolution* ([arXiv:2606.14314](https://arxiv.org/abs/2606.14314)): treats "when/how should the agent talk to the user" as an explicit, evolvable *communication policy* (prompt-level, no fine-tuning) and shows policy refinement alone improves task success — a natural fit for a prompt-file-based platform.

**Synthesis.** Initiative is a two-stage decision: a cheap, always-on *trigger* model over structured events, then an expensive *content* model invoked only after wake — with the final fire/suppress choice framed as Horvitz-style expected utility including attention state. Frontier LLMs are mediocre at the trigger decision (≈66% F1 fine-tuned; 40% end-to-end proactive problem-solving), so the platform should not rely on raw LLM judgment for it.

---

## 2. Sleep-time and offline compute beyond arXiv:2504.13171

- **The anchor result** (*Sleep-time Compute*, [arXiv:2504.13171](https://arxiv.org/abs/2504.13171), [code](https://github.com/letta-ai/sleep-time-compute)): pre-processing context during idle time reduces test-time compute needed for equal accuracy **~5×**, raises accuracy up to **+13% / +18%** (Stateful GSM-Symbolic / Stateful AIME) when sleep-time compute is scaled, and **amortizes to 2.5× lower average cost per query** when multiple queries share a context. Crucially, gains correlate with **query predictability** — sleep-time compute pays when you can anticipate what will be asked.
- ***Do Language Models Need Sleep? Offline Recurrence for Improved Online Inference*** ([arXiv:2605.26099](https://arxiv.org/abs/2605.26099)): architectural follow-up — offline recurrent passes consolidate accumulated context into persistent fast weights (SSM blocks), letting the model clear its KV cache while preserving reasoning; gains grow with "sleep" duration and concentrate on deeper-reasoning examples. Validates the consolidate-then-forget pattern the consolidation jobs implement, at the representation level.
- **Memory-consolidation variants:** *SCM: Sleep-Consolidated Memory with Algorithmic Forgetting* ([arXiv:2604.20943](https://arxiv.org/abs/2604.20943)) couples offline consolidation with principled forgetting; PRIME (above) does consolidation into procedural memory. A cautionary result: *Remembering More, Risking More* ([arXiv:2605.17830](https://arxiv.org/abs/2605.17830)) shows longitudinal safety risk *grows* with accumulated memory in memory-equipped agents — consolidation pipelines need a safety/PII filter, not just a relevance filter.
- **Speculative/anticipatory execution for agents:** **AgenticCache** ([arXiv:2604.24039](https://arxiv.org/abs/2604.24039)) exploits *plan locality* — the next plan is largely predictable from the current one — with a runtime plan-transition cache validated asynchronously by a background updater: **+22% success rate, −65% latency, −50% tokens** across 12 configurations. At the serving layer, speculative prefetching of predicted KV entries reaches **95% prefetch accuracy** and 3.2× throughput (CXL-SpecKV, [arXiv:2512.11920](https://arxiv.org/abs/2512.11920)); retrieval-augmented speculative decoding (RACER, [arXiv:2604.14885](https://arxiv.org/abs/2604.14885)) follows the same "amortize predictable work offline" logic.
- **ProAct** ([arXiv:2605.25971](https://arxiv.org/abs/2605.25971)) is the bridge between areas 1 and 2: idle-time compute used specifically to *prefetch answers to predicted queries* (evidence gathering before the user asks), with the −28.1% hallucination number above.

**Synthesis.** The literature now supports three distinct offline-compute products, all schedulable by the idle detector: (1) *consolidation* (memory rewrite + forgetting), (2) *anticipation* (predict likely next queries from episodic memory; pre-compute briefings/answers; 2.5× amortization only when predictions are good — log prediction hit-rate), (3) *speculative plan caching* for recurring skills (routine runs hitting a validated plan cache instead of full replanning).

---

## 3. Event-driven and trigger-based agents

**Trigger-action programming (TAP) — the pre-LLM evidence base:**

- Ur et al., *Trigger-Action Programming in the Wild* (CHI '16, [DOI 10.1145/2858036.2858556](https://doi.org/10.1145/2858036.2858556), [dataset](https://www.upod.io/datasets.html)): analysis of **224,590 shared IFTTT recipes from >100,000 users** — single trigger→single action covers a very large share of real automation demand, but composed rules produce emergent behaviors users can't diagnose.
- Huang & Cakmak (UbiComp '15, [DOI 10.1145/2750858.2805830](https://doi.org/10.1145/2750858.2805830)): users systematically **confuse instantaneous *events* with protracted *states*** ("when I arrive home" vs "while I am home"), causing rules that fire wrongly; mental-model mismatch between intent, expression, and rule semantics is the dominant TAP bug class ([follow-up: How Users Interpret Bugs in TAP, CHI '19](https://www.blaseur.com/papers/chi19-iftt-cameraready.pdf)).
- Mi et al., *An Empirical Characterization of IFTTT* (IMC '17, [DOI 10.1145/3131365.3131369](https://doi.org/10.1145/3131365.3131369)): 6-month measurement; **polling-based triggers and cloud intermediation dominate end-to-end latency** and waste, motivating push/webhook-first watcher design (see also RT-IFTTT's condition-aware adaptive polling, [DOI 10.1109/RTSS.2017.00028](https://ieeexplore.ieee.org/document/8277299/)).
- LLMs are now used to *author* TAP rules from natural language ([arXiv:2310.15024](https://arxiv.org/abs/2310.15024); FARM, [arXiv:2601.15687](https://arxiv.org/abs/2601.15687)) — i.e., NL → typed rule at creation time, deterministic engine at run time.

**Semantic event detection and streaming LLM inference:**

- **VectraFlow** ([arXiv:2604.03855](https://arxiv.org/abs/2604.03855)): a semantic streaming dataflow engine — six continuous semantic operators (filter/map/aggregate/join/group/window) each with **configurable LLM / embedding / hybrid implementations** trading throughput vs accuracy, plus a semantic event-pattern operator combining LLM extraction with **finite automata** for temporal patterns. NL intents compile into executable operator graphs. This is the cleanest published architecture for "watcher agents" over feeds.
- **Signal-Driven Observation** ([arXiv:2606.06708](https://arxiv.org/abs/2606.06708), ICML '26 FAGEN workshop): decouple observation from action — re-observe only when signals fire (URL transitions, new interactive elements, failures, exogenous events) instead of ingesting tens of thousands of observation tokens per step.
- **SentinelBench** ([arXiv:2606.05342](https://arxiv.org/abs/2606.05342)): 100 monitoring tasks across 10 synthetic live web environments (email, calendar, finance...) with scripted event sequences; scores agents on **task completion × reaction time × resource utilization** and shows harness design choices dominate the responsiveness/cost trade-off. It is the closest existing benchmark to this platform's ambient mode and a template for a replayable event-sequence test harness.

**Synthesis.** The 25-year TAP record says: keep the trigger layer *deterministic, typed, and cheap* (events vs states as distinct types; push over polling), use the LLM at rule-authoring time to compile "watch for X" into structured conditions, and reserve streaming LLM inference for semantic predicates that structured matching can't express — with embedding-based pre-filters before any LLM call (VectraFlow's hybrid operators).

---

## 4. Interruption science: what transfers to push-vs-digest decisions

**Cost of interruption and breakpoints:**

- Iqbal & Bailey's defer-to-breakpoint line (CHI '08, [DOI 10.1145/1357054.1357070](https://doi.org/10.1145/1357054.1357070)): interruption cost (resumption lag, errors, frustration) is lowest at **coarse task breakpoints**; statistical breakpoint detectors reached 52–59% recall / 64% precision on real tasks, and intelligent notification management measurably reduced disruption.
- **Attelia** (Okoshi et al., PerCom '15, [DOI 10.1109/PERCOM.2015.7146515](https://doi.org/10.1109/PERCOM.2015.7146515)): deferring phone notifications to detected breakpoints cut cognitive load **46% in-lab and 33% in a 16-day, 30-participant field study** vs randomly timed delivery.
- **InterruptMe** (Pejovic & Musolesi, UbiComp '14, [DOI 10.1145/2632048.2632062](https://doi.org/10.1145/2632048.2632062)): opportune interruption moments predictable from phone context at **~60% accuracy** (sentiment: 64% precision / 41% recall) — context-based interruptibility prediction works but is noisy; design for graceful wrongness.
- Mark et al. (CHI '08, [DOI 10.1145/1357054.1357072](https://doi.org/10.1145/1357054.1357072)): interrupted workers compensate by working faster at the cost of significantly higher stress, frustration, and time pressure — interruption cost is partly *invisible* in throughput metrics; subjective load must be measured too.

**Batching and digests:**

- Fitz, Kushlev et al., *Batching smartphone notifications can improve well-being* (*Computers in Human Behavior* 2019, [DOI 10.1016/j.chb.2019.07.016](https://doi.org/10.1016/j.chb.2019.07.016)): randomized field experiment, **N=237**. Batching to **three predictable deliveries per day** beat both as-they-arrive and never-deliver on attentiveness, mood, productivity, and sense of control; total suppression *backfired* (higher anxiety and FoMO). The actionable shape: predictable digest cadence with an escape hatch for urgent items, not silence.
- The *Intelligent Notification Systems* survey ([arXiv:1711.10171](https://arxiv.org/abs/1711.10171)) consolidates the field: the two learnable quantities are *opportune moment* and *notification value*, and both should modulate delivery.

**Learning the timing policy:**

- Duolingo's sleeping/recovering bandit for recurring notifications (Yancey & Settles, KDD '20, [PDF](https://research.duolingo.com/papers/yancey.kdd20.pdf)) demonstrates that Thompson-sampling bandits over notification choice/timing yield small-but-real engagement lifts at scale (order 0.5–1.5%), with "recovering" arms modeling burnout from repetition — a direct template for learning per-user digest timing from the runs ledger.
- Mozannar et al., *When to Show a Suggestion?* (AAAI '24, [arXiv:2306.04930](https://arxiv.org/abs/2306.04930)): with data from **535 programmers**, a cascade predicting acceptance probability can hide a significant fraction of would-be-rejected Copilot suggestions; ablations show the user's *latent state* matters; and — key warning — **optimizing purely for suggestion acceptance degrades suggestion quality** (acceptance is a biased reward).

**What transfers to an LLM concierge.** Almost everything: (1) interruption is a cost paid from a budget, (2) breakpoint delivery is the cheapest big win — and the platform already has an idle-detector concept, which is precisely a breakpoint detector, (3) a predictable digest with an urgency bypass is the empirically best default, (4) timing policies can be learned online from accept/dismiss signals, provided acceptance isn't the *only* reward term.

---

## 5. Standing intents and continual queries

**Streams heritage.** Standing intents are continuous queries: CQL (Arasu, Babu & Widom, *VLDB Journal* 2006, [DOI 10.1007/s00778-004-0147-z](https://doi.org/10.1007/s00778-004-0147-z)) established the stream/relation/window algebra; stream reasoning (LARS, Temporal Datalog, [arXiv:1711.04013](https://arxiv.org/abs/1711.04013)) added logic over windows. The durable lesson: a standing query is a *registered, typed object with window semantics* evaluated incrementally — not a prompt re-asked on a timer. VectraFlow ([arXiv:2604.03855](https://arxiv.org/abs/2604.03855)) is the modern LLM-native instantiation.

**Prospective memory in LLM agents — the 2026 results are sobering:**

- **PM-Bench** ([arXiv:2607.12385](https://arxiv.org/html/2607.12385v1)): adapts the Virtual Week paradigm — 7 simulated days, 80 steps, agents must keep doing foreground work while firing deferred intentions on time. Best result: **65.1% macro Set-F1 (GPT-5.4 with heartbeat)**; **non-clock proactive monitoring hit rate only 16.7%**; cross-day tasks ≤**50%** hit rate; rescheduled/updated tasks ≤**47.2%**. Failure modes: precision/recall trade-off (recover more → false-alarm more), deferred-intention decay under distraction, update insensitivity. Even a hierarchical agent issuing 1,661 monitoring queries didn't solve hidden-channel monitoring.
- **TriggerBench** ([arXiv:2606.23459](https://arxiv.org/abs/2606.23459)): 1,265 prospective-memory tasks; retrospective memory near-saturates to 100K tokens while **prospective memory decays sharply with context length**; models fall into an "always-remind" heuristic (recall via spam); PM accuracy tracks *spare reasoning capacity*, collapsing when the foreground task is hard.
- **TemporalBench** ([arXiv:2602.13272](https://arxiv.org/abs/2602.13272)): strong numeric forecasting does not transfer to event-aware temporal reasoning — "remind me when X" conditions need explicit machinery, not model priors.

**Goal persistence over long horizons.** The Horizon Gap survey ([arXiv:2608.06663](https://arxiv.org/abs/2608.06663), 1,547 papers reviewed) identifies **goal drift** as one of three consistent long-horizon failure classes; *Inherited Goal Drift* ([arXiv:2603.03258](https://arxiv.org/html/2603.03258)) shows contextual pressure alone induces drift; *Push Your Agent* ([arXiv:2605.23574](https://arxiv.org/pdf/2605.23574)) shows agents stop before quantitative goals are verifiably met unless persistence is enforced; UltraHorizon ([arXiv:2509.21766](https://arxiv.org/abs/2509.21766)) attributes long-run failure to "in-context locking."

**Synthesis.** Do not store a standing intent inside a model context and hope the model remembers. Every published measurement says in-context prospective memory degrades with horizon length, foreground load, and updates. Standing intents belong in Postgres as first-class rows — typed condition (event vs state vs time), window spec, check cadence, last-evaluated watermark, expiry — with the scheduler (not the LLM) responsible for *when to check*, and the LLM responsible only for evaluating semantic predicates and composing the resulting message.

---

## 6. Autonomy levels and safety for unattended agents

**Autonomy frameworks:**

- *Levels of Autonomy for AI Agents* ([arXiv:2506.12469](https://arxiv.org/abs/2506.12469)): SAE-style **L1 (human controls) → L2 (agent proposes, human approves each action) → L3 (agent acts within guardrails, human can override) → L4 (human sets objectives, monitors outcomes) → L5 (full autonomy)**, plus "autonomy certificates" — explicit, auditable declarations of which tasks a system may perform at which level. Core principle: match autonomy to risk per task, never as a blanket setting.
- *Measuring AI Agent Autonomy* ([arXiv:2502.15212](https://arxiv.org/abs/2502.15212)): autonomy scored *statically from orchestration code* along **impact** and **oversight** dimensions — meaning registry metadata can carry the autonomy classification and be audited without running anything.
- Surveys of autonomy-induced risk ([arXiv:2506.23844](https://arxiv.org/html/2506.23844v1)) and agentic-safety benchmarks ([arXiv:2605.16282](https://arxiv.org/html/2605.16282v1)) both conclude risk scales with action reach (email, shell, money) far more than with model capability per se.

**Action-risk measurement:**

- **ToolEmu** (ICLR '24 Spotlight, [arXiv:2309.15817](https://arxiv.org/abs/2309.15817)): LM-emulated sandbox, 36 high-stakes toolkits / 144 cases; **68.8%** of automatically flagged failures were validated as real by humans, and **even the safest tested agent produced risky failures 23.9% of the time** — with the dominant threat model being *benign but underspecified instructions*, exactly the standing-intent situation. **R-Judge** ([arXiv:2401.10019](https://arxiv.org/abs/2401.10019)) shows LLMs are also mediocre at *recognizing* risk in trajectories, so a separate judge model is a weak sole safeguard.
- *Check Yourself Before You Wreck Yourself* ([arXiv:2510.16492](https://arxiv.org/abs/2510.16492)): merely giving agents an explicit **quit/abstain option** improved ToolEmu safety **+0.39 on a 0–3 scale (+0.64 for proprietary models)** at a negligible **−0.03** helpfulness cost across 12 LLMs — the cheapest known safety intervention for unattended runs.

**HITL under load — approval is a finite resource:**

- *Oversight Has a Capacity* ([arXiv:2606.08919](https://arxiv.org/abs/2606.08919)): models the human approver as **fatiguing and subjective** (inter-reviewer agreement on riskiness only **Fleiss' κ = 0.52**). Result: safety vs escalation-rate follows an **inverted U** — beyond the reviewer's capacity, *more* escalation makes the system *less* safe, and a load-aware selective-escalation policy both maximizes realized safety and **resists flooding attacks** designed to induce rubber-stamping (a threat OWASP's agentic-AI guide lists explicitly as "Overwhelming HITL").

**Runaway loops and cost containment:**

- *When Agents Do Not Stop: Infinite Agentic Loops* ([arXiv:2607.01641](https://arxiv.org/abs/2607.01641)): defines Infinite Agentic Loops (unbounded model-call/tool/handoff cycles), consequences (cost exhaustion, context growth, **repeated external side effects**), and ships IAL-Scan, a static analyzer that finds unbounded feedback paths in real agent projects. Practitioner reports document runaway multi-agent loops burning tens of thousands of dollars over days; the operational signature is **sustained token-consumption velocity without task progress**, arguing for rate-based (tokens/minute) monitors alongside absolute caps.

**Synthesis.** For unattended operation: classify every registry tool/skill with an autonomy ceiling (L2/L3/L4) derived from impact+reversibility; enforce at run time, not prompt time; treat approvals as a budget with selective escalation and digest-batched low-risk approvals; give every ambient loop an abstain action and hard step/token/wall-clock/side-effect budgets recorded in the runs ledger.

---

## 7. Evaluating ambient/proactive behavior

- **Intervention quality as precision/recall.** ProactiveBench's accept/reject-trained reward model ([arXiv:2410.12361](https://arxiv.org/abs/2410.12361)) and PM-Bench's **Set-level F1 with explicit false-alarm accounting** ([arXiv:2607.12385](https://arxiv.org/html/2607.12385v1)) are the two reusable patterns: score *fire/hold* decisions against human accept/dismiss labels, and penalize over-firing symmetrically with misses (TriggerBench's contrastive negatives exist precisely to catch the "always-remind" degenerate policy, [arXiv:2606.23459](https://arxiv.org/abs/2606.23459)).
- **The SentinelBench triad** ([arXiv:2606.05342](https://arxiv.org/abs/2606.05342)) — completion × reaction time × resource utilization over scripted event sequences in live environments — is the right harness shape for ambient mode: deterministic, replayable event scripts; measure both *whether* and *how fast* the watcher fired, and what it cost.
- **Long-horizon stress tests:** UltraHorizon ([arXiv:2509.21766](https://arxiv.org/abs/2509.21766); 200k+ tokens, 400+ tool calls; humans beat all agents; failures rooted in in-context locking) and the broader evaluation survey ([arXiv:2507.21504](https://arxiv.org/pdf/2507.21504)) both note that short-horizon evals cannot detect drift, context loss, or cumulative-decision effects — ambient evals must run over multi-day simulated clocks.
- **Annoyance is measurable:** the HCI results give the instrument set — subjective load (NASA-TLX-style, as in Attelia's 33–46% reductions), stress/frustration (Mark et al.), perceived control/attentiveness (Fitz et al. N=237), and condition-level preference (Need Help? CHI '25). ProactiveEval ([arXiv:2508.20973](https://arxiv.org/abs/2508.20973)) shows automatic evaluation-environment generation is viable for scaling this.
- **Reward pitfalls:** Mozannar et al. ([arXiv:2306.04930](https://arxiv.org/abs/2306.04930)) — optimizing acceptance alone degrades content quality; Duolingo's recovering-bandit ([KDD '20](https://research.duolingo.com/papers/yancey.kdd20.pdf)) — repeated identical interventions have *decaying* reward, so the evaluator must model burnout.

---

## Design rules for ambient mode (evidence-backed)

1. **Score every proactive act as expected utility with an explicit interruption cost term.** Persist a per-notification record `{value_estimate, attention_state, cost_estimate, decision}` in the runs ledger; fire only when value − cost > threshold. This is Horvitz CHI '99 ([DOI 10.1145/302979.303030](https://doi.org/10.1145/302979.303030)) made auditable, and the N=32 timing study ([arXiv:2602.00880](https://arxiv.org/abs/2602.00880), +21% accuracy from timing alone) shows the cost term is not decoration.

2. **Split "when to wake" from "what to do" — and never make the wake decision an LLM call per event.** A cheap deterministic/embedding trigger layer scores events; the LangGraph skill loop runs only after wake. Evidence: temporal-graph triggers beat LLM triggers by +16.7 F1 at 12–83× lower cost ([arXiv:2605.30152](https://arxiv.org/abs/2605.30152)); LlamaPIE's small-decides/large-speaks pipeline won user preference ([arXiv:2505.04066](https://arxiv.org/abs/2505.04066)). In this stack: SQL/typed matchers + optional pgvector similarity as tier 1, single small-model classification as tier 2, full skill run as tier 3.

3. **Silence is the default; track intervention precision as a first-class metric.** Frontier models over-fire (TriggerBench "always-remind" collapse, [arXiv:2606.23459](https://arxiv.org/abs/2606.23459)) and under-perform on fire/hold (66.47% F1 *after* fine-tuning, [arXiv:2410.12361](https://arxiv.org/abs/2410.12361)). Log accept/dismiss/ignore per intervention; alert when rolling precision drops.

4. **Default delivery is a predictable digest (~2–3 scheduled flushes/day) with an urgency bypass; never total suppression.** Fitz et al.'s N=237 RCT: 3×/day batching improved mood, productivity, and control, while zero-delivery raised anxiety/FoMO ([DOI 10.1016/j.chb.2019.07.016](https://doi.org/10.1016/j.chb.2019.07.016)). Implement as a `deliveries` outbox table drained on schedule, with `urgency >= interrupt_threshold` bypassing the batch.

5. **Reuse the idle detector as a breakpoint detector for delivery, not just consolidation.** Delivering at activity breakpoints cut cognitive load 46% (lab) / 33% (field) in Attelia ([DOI 10.1109/PERCOM.2015.7146515](https://doi.org/10.1109/PERCOM.2015.7146515)) and is the core Iqbal–Bailey result ([DOI 10.1145/1357054.1357070](https://doi.org/10.1145/1357054.1357070)). When the user returns from idle or finishes a conversation turn-burst, flush pending non-urgent items.

6. **Standing intents are typed Postgres rows, not remembered prompts.** Prospective memory in-context decays sharply with horizon and load — best agents hit 65.1% Set-F1, 16.7% on non-clock monitoring ([arXiv:2607.12385](https://arxiv.org/html/2607.12385v1); [arXiv:2606.23459](https://arxiv.org/abs/2606.23459)). Schema per CQL semantics ([DOI 10.1007/s00778-004-0147-z](https://doi.org/10.1007/s00778-004-0147-z)): condition type, window, watermark, expiry, check cadence; the scheduler decides *when to evaluate*, the LLM only evaluates semantic predicates and drafts the message.

7. **Distinguish event triggers from state triggers at the schema level, and compile NL rules at creation time.** Event/state confusion is the dominant TAP bug class (Huang & Cakmak, [DOI 10.1145/2750858.2805830](https://doi.org/10.1145/2750858.2805830)); LLMs should translate "remind me when…" into a typed rule once, echo the interpretation back for confirmation (Horvitz's dialog-to-resolve-uncertainty), and the runtime engine executes it deterministically ([arXiv:2310.15024](https://arxiv.org/abs/2310.15024)).

8. **Watchers observe on signals, not polls; LLM calls sit behind structured and embedding pre-filters.** Polling dominated IFTTT's latency/waste ([DOI 10.1145/3131365.3131369](https://doi.org/10.1145/3131365.3131369)); signal-driven observation decouples observation from action ([arXiv:2606.06708](https://arxiv.org/abs/2606.06708)); VectraFlow's hybrid operators formalize the throughput/accuracy dial ([arXiv:2604.03855](https://arxiv.org/abs/2604.03855)). In one asyncio process: webhook/event ingestion into an `events` table + LISTEN/NOTIFY, with per-watcher cheap predicates before any model call.

9. **Point sleep-time compute at *predicted* queries and measure prediction hit-rate.** The 5× compute saving and 2.5× amortization materialize only when queries are predictable ([arXiv:2504.13171](https://arxiv.org/abs/2504.13171)); idle-time anticipation cut turns 14.8% and hallucinations 28.1% ([arXiv:2605.25971](https://arxiv.org/abs/2605.25971)). Consolidation jobs should emit "likely next asks" from episodic memory, pre-compute briefings for them, and record whether each precomputation was ever used — prune anticipation for users/contexts where hit-rate is low.

10. **Cache plans for recurring routines; validate the cache in the background.** Routine runs exhibit plan locality; a plan-transition cache with an async background validator gave +22% success, −65% latency, −50% tokens ([arXiv:2604.24039](https://arxiv.org/abs/2604.24039)). Store validated plans in procedural memory keyed by (skill, context signature); consolidation jobs are the natural cache updater.

11. **Autonomy level is per-registry-record metadata, enforced at the gate, auditable statically.** Tag every tool/skill with an L1–L5 ceiling from impact × reversibility ([arXiv:2506.12469](https://arxiv.org/abs/2506.12469)), scoreable from orchestration code alone ([arXiv:2502.15212](https://arxiv.org/abs/2502.15212)). Ambient runs execute at ≤L3 (act within guardrails) for reversible actions and drop to L2 (propose, queue for approval) for irreversible/external-side-effect actions — because even the safest agents fail risky cases 23.9% of the time under underspecified instructions ([arXiv:2309.15817](https://arxiv.org/abs/2309.15817)).

12. **Treat human approval as a fatiguing, finite budget: escalate selectively and batch low-risk approvals into the digest.** Safety vs escalation rate is an inverted U (κ=0.52 reviewer agreement; over-escalation *reduces* realized safety and enables flooding-induced rubber-stamping, [arXiv:2606.08919](https://arxiv.org/abs/2606.08919)). HITL gates should carry a per-day escalation budget and rank pending approvals by risk, not FIFO.

13. **Every ambient loop gets an explicit abstain/quit action and hard budgets.** Quit instructions alone: +0.39 safety (0–3 scale) for −0.03 helpfulness across 12 models ([arXiv:2510.16492](https://arxiv.org/abs/2510.16492)). Budgets per run: max steps, max tokens, max wall-clock, max external side effects — plus a tokens-per-minute-without-progress monitor, since unbounded feedback paths are a documented failure class with real multi-day cost blowouts ([arXiv:2607.01641](https://arxiv.org/abs/2607.01641)). The runs ledger already has the right shape to enforce and audit this.

14. **Learn the timing/channel policy online from ledger feedback — but never use acceptance as the sole reward.** Bandit-style optimization of notification timing works (Duolingo's sleeping/recovering bandit, [KDD '20](https://research.duolingo.com/papers/yancey.kdd20.pdf)) and acceptance-probability gating can suppress a large fraction of doomed suggestions (535-programmer CDHF study), but pure acceptance-reward measurably degrades content quality ([arXiv:2306.04930](https://arxiv.org/abs/2306.04930)) and repeated identical interventions have decaying reward. Blend acceptance with downstream-usefulness and explicit-dismissal penalties.

15. **Evaluate ambient mode on a replayable simulated clock with scripted event sequences, scoring the fire/hold decision and the cost side by side.** Adopt SentinelBench's triad (completion × reaction time × resource use, [arXiv:2606.05342](https://arxiv.org/abs/2606.05342)) plus PM-Bench-style Set-F1 with false-alarm accounting ([arXiv:2607.12385](https://arxiv.org/html/2607.12385v1)) over multi-day scenarios (short evals cannot see drift — [arXiv:2608.06663](https://arxiv.org/abs/2608.06663), [arXiv:2509.21766](https://arxiv.org/abs/2509.21766)). Because memory accumulation itself raises longitudinal risk ([arXiv:2605.17830](https://arxiv.org/html/2605.17830v1)), include a consolidation-safety check (no secrets/PII promoted to semantic memory) in the eval suite — it fits naturally next to the existing doclint gate.

---

## Highest-value reads (if the design doc cites only ten)

1. Horvitz, *Principles of Mixed-Initiative User Interfaces*, CHI '99 — [DOI 10.1145/302979.303030](https://doi.org/10.1145/302979.303030)
2. *Sleep-time Compute*, [arXiv:2504.13171](https://arxiv.org/abs/2504.13171) (5×/2.5× numbers; predictability caveat)
3. *Proactive Agent* + ProactiveBench, [arXiv:2410.12361](https://arxiv.org/abs/2410.12361) (66.47% F1 ceiling on fire/hold)
4. *Do Proactive Agents Really Need an LLM to Decide When to Wake?*, [arXiv:2605.30152](https://arxiv.org/abs/2605.30152) (cheap trigger layer wins)
5. Fitz et al., notification batching RCT, [DOI 10.1016/j.chb.2019.07.016](https://doi.org/10.1016/j.chb.2019.07.016) (3×/day digest; suppression backfires)
6. Attelia, [DOI 10.1109/PERCOM.2015.7146515](https://doi.org/10.1109/PERCOM.2015.7146515) (breakpoint delivery, −33–46% load)
7. PM-Bench, [arXiv:2607.12385](https://arxiv.org/html/2607.12385v1) + TriggerBench, [arXiv:2606.23459](https://arxiv.org/abs/2606.23459) (don't trust in-context prospective memory)
8. ToolEmu, [arXiv:2309.15817](https://arxiv.org/abs/2309.15817) (23.9% risky-failure floor under underspecification)
9. *Oversight Has a Capacity*, [arXiv:2606.08919](https://arxiv.org/abs/2606.08919) (approval fatigue; inverted-U safety)
10. SentinelBench, [arXiv:2606.05342](https://arxiv.org/abs/2606.05342) (how to benchmark a watcher)

*Caveat on recency: several 2026 papers cited above (arXiv 26xx IDs) are preprints or workshop papers; quantitative claims were taken from their abstracts/HTML versions as of 2026-08-25 and should be re-verified against final versions before external publication of the design doc.*
