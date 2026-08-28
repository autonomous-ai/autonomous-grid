# Pattern lineage — from research names to simple local-first patterns

The concise catalog did not discard the earlier work. It separated structural
patterns from statistical implementations and gave each surviving idea a
shorter name. This map accounts for every entry in the original 27-pattern
research set.

| Original research entry | Local-first home | Decision |
|---|---|---|
| [1. Mate-in-One](portable_patterns.md#1-mate-in-one--pick-the-best-fit) | **Best Fit** | renamed; the single-route baseline remains a pattern |
| [2. Fan-Out](portable_patterns.md#2-fan-out--same-prompt-n-answers-a-vote) | **Vote** | renamed and kept |
| [3. Master / Slave](portable_patterns.md#3-master--slave--a-planner-splits-the-job) | **Split Work** | renamed to describe the collaboration directly |
| [4. Adversarial](portable_patterns.md#4-adversarial--two-careful-reads-a-judge) | **Challenge** | renamed and kept |
| [5. Strategy](portable_patterns.md#5-strategy--compile-a-compatible-orchestration-plan) | **Recipe Router** | renamed around the visible choice of workflow |
| [6. Brute-Force](portable_patterns.md#6-brute-force--many-approaches-keep-the-best) | **Brute Force** | kept as the flagship local-first abundance pattern |
| [7. Ensemble](portable_patterns.md#7-ensemble--same-prompt-keep-the-average) | **Ensemble** | kept |
| [8. Verifier Gate](portable_patterns.md#8-verifier-gate--one-draft-a-check-retry-on-fail) | **Check and Retry** | renamed around its visible loop |
| [9. Debate](portable_patterns.md#9-debate--two-reads-that-loop-until-they-agree) | **Challenge** | retained as the bounded multi-round variant |
| [10. Pipeline](portable_patterns.md#10-pipeline--each-step-consumes-the-last) | **Pipeline** | kept |
| [11. Negative Selection](portable_patterns.md#11-negative-selection--force-divergence-before-you-judge) | **Diversity Gate** | renamed around its useful move: reject correlated copies |
| [12. Markowitz Ensemble](portable_patterns.md#12-markowitz-ensemble--correlation-weighted-not-just-averaged) | **Ensemble** | correlation weighting becomes an Ensemble refinement |
| [13. PID Confidence Loop](portable_patterns.md#13-pid-confidence-loop--a-budget-that-tracks-error-history-and-trend) | **Adaptive Effort** | control theory becomes one possible budget controller |
| [14. Pheromone Router](portable_patterns.md#14-pheromone-router--learn-which-shape-wins-with-decay) | **Routing Memory** | reinforcement and decay become implementation choices |
| [15. Byzantine Adjudicator](portable_patterns.md#15-byzantine-adjudicator--spend-more-when-the-disagreement-is-adversarial) | **Tiebreaker** | simplified to “add independent evidence when camps split” |
| [16. Straggler Backup](portable_patterns.md#16-straggler-backup--duplicate-only-the-overdue-worker) | **Straggler Backup** | kept |
| [17. Materialized Answer](portable_patterns.md#17-materialized-answer--cache-the-verified-answer-by-a-semantic-key) | **Answer Cache** | renamed in ordinary language |
| [18. Canary Trust-Equity](portable_patterns.md#18-canary-trust-equity--earn-a-vote-before-you-ever-cast-one) | **Shadow Model** | renamed around the read-only trust-building move |
| [19. CVaR Budgeting](portable_patterns.md#19-cvar-budgeting--size-the-spend-by-the-tail-not-the-mean) | **Risk Ladder** | tail-risk math becomes one way to size the ladder |
| [20. Circuit Breaker + Bulkhead](portable_patterns.md#20-circuit-breaker--bulkhead--fail-fast-quarantine-the-toxic-class) | **Circuit Breaker** | circuit breaking kept; isolation remains a deep-reference refinement |
| [21. Delphi Consensus](portable_patterns.md#21-delphi-consensus--anonymous-rounds-iterated-until-the-spread-closes) | **Blind Estimate** | renamed around the anti-anchoring mechanism |
| [22. Trial Sequential Analysis](portable_patterns.md#22-trial-sequential-analysis--policy-changes-only-at-registered-evidence-looks) | **Shadow Model** | retained as its predeclared evidence gate |
| [23. Evidence-Bar Ladder](portable_patterns.md#23-evidence-bar-ladder--proof-threshold-scales-with-the-cost-of-error) | **Risk Ladder** | merged with consequence-based budgeting |
| [24. Type-Revelation Screening](portable_patterns.md#24-type-revelation-screening--probe-a-models-type-in-idle-before-trust) | **Model Audition** | renamed around what the operator actually does |
| [25. Condorcet Pairwise Pooling](portable_patterns.md#25-condorcet-pairwise-pooling--head-to-head-beats-plurality-on-a-three-way-split) | **Tiebreaker** | pairwise comparison becomes a split-vote refinement |
| [26. Slack-Stealing Scheduler](portable_patterns.md#26-slack-stealing-scheduler--run-background-work-only-in-the-idle-a-live-request-leaves-free) | **Idle Worker** and **Night Shift** | split into safe idle scheduling and verified staged improvement |
| [27. Thompson Posterior Router](portable_patterns.md#27-thompson-posterior-router--route-by-sampling-each-models-posterior-not-by-argmax) | **Routing Memory** | exploration becomes a deep-reference refinement of remembered routing |

## Earlier focused-catalog entries

The previous six-pattern main page also remains represented:

| Earlier entry | Local-first home |
|---|---|
| A1. Brute-Force Search | **Brute Force** |
| A2. Bounded Verify-and-Repair | **Check and Retry** |
| A3. Diverse Council | **Vote**, **Challenge**, and **Diversity Gate** |
| F1. Model Artifact Contract | **Pinned Model** |
| L1. Resident-Set Planner | **Fit the Box** and **Keep It Warm** |
| F2. Boundary-Compiled Graph | **Privacy Boundary** |
| L2. Verified Night Shift | **Idle Worker** and **Night Shift** |
| L3. Energy Envelope | **Power Budget** |
| Sovereign Island composition | **Offline Island** |

**Local Cascade**, **Data Stays Put**, and **Private Memory** are new. They
capture three local-first pressures the earlier catalogs left implicit: the
owned path should be the default; inference should move to private data instead
of centralizing it; and long-lived personal context should remain owned,
offline-capable, and scoped before it reaches a model.

The detailed original entries remain in the
[research reference](portable_patterns.md); the physical contracts from the
focused catalog remain in the
[archived six-pattern engineering reference](six_pattern_reference.md).
