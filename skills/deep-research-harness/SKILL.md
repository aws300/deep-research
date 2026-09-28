---
name: deep-research-harness
description: Citation-tracked deep research workflow for Amazon Bedrock AgentCore harness. Use for any request that asks to research, investigate, compare, survey or produce an evidence-backed report. Retrieval uses the Gateway WebSearch tool (parallel calls), evidence is persisted with the bundled scripts, output is a validated Markdown report with numbered citations.
---

# Deep Research (harness edition)

You run inside an isolated microVM with `shell`, `file_operations`, and the Gateway tool `WebSearch`
(fully qualified name looks like `research_tools___web-search___WebSearch` or `web-search___WebSearch`;
discover it from your tool list). Scripts live in this skill's `scripts/` directory; find it with:

```bash
SKILL_DIR=/tmp/research/tools   # pre-installed by the service before your first turn
test -f "$SKILL_DIR/evidence_store.py" || SKILL_DIR=$(dirname "$(find / -name evidence_store.py -not -path '/proc/*' 2>/dev/null | head -1)")
```
If the scripts are missing entirely, keep sources/evidence in `$RUN_DIR/sources.jsonl` and `$RUN_DIR/evidence.jsonl`
by hand (one JSON object per line with url, title, quote) and do the validation checklist manually.

All work for a task lives in `RUN_DIR=<workspace>/<task_id>` (the task prompt gives both values).

## Non-negotiables
1. Every factual claim in the report carries an inline citation `[N]` whose source is in `## Bibliography`.
2. Never fabricate a URL, title, date or quote. Only cite results returned by `WebSearch`.
3. Persist sources and evidence with the scripts *before* writing prose.
4. Keep tool results compact: request `maxResults` 8–10; summarise, don't paste whole results into the report.
5. Finish with the `DR_STATUS {...}` line exactly as requested by the task prompt.

## Workflow (8 phases, adapt depth to quick / standard / deep)

### 1. SCOPE (no tools)
Write 3–6 lines: the question, audience, time window (default: last 18 months), out-of-scope items,
and the assumptions you are making. Save as `$RUN_DIR/scope.md`.

### 2. PLAN
Decompose into 4–8 independent sub-questions (core concept, technical detail, recent developments,
alternatives/criticism, quantitative data, practitioner experience). Save `$RUN_DIR/plan.md`.
```bash
mkdir -p "$RUN_DIR" && python3 "$SKILL_DIR/citation_manager.py" init-run --out-dir "$RUN_DIR" --query "<question>" --mode <quick|standard|deep>
```

### 3. RETRIEVE (parallel)
For each sub-question craft 1–3 distinct queries (<= 200 chars; include a year such as 2026 for recency;
use `filters.publishedDateFilter` / `filters.domainFilter` when the task asks for trusted or recent sources).
**Call `WebSearch` for all independent queries in the same turn** so they execute concurrently.
Targets: quick 6–10 calls, standard 15–25, deep 30–45. Follow up on gaps with 2–5 more calls.

After each batch, register the sources you will actually use and store the exact quote you rely on:
```bash
python3 "$SKILL_DIR/citation_manager.py" register-source --dir "$RUN_DIR" --json '{"raw_url":"https://...","title":"...","year":"2026","source_type":"industry"}'
# -> prints {"source_id": "..."}; then
python3 "$SKILL_DIR/evidence_store.py" add --dir "$RUN_DIR" --json '{"source_id":"<id>","quote":"<exact text from the result snippet>","evidence_type":"direct_quote","retrieval_query":"<query>"}'
```
Batch several `register-source` / `add` calls in one shell invocation to save iterations.

### 4. TRIANGULATE
Core claims need >= 2 independent sources (different domains). Note contradictions explicitly;
prefer official documentation / primary sources over secondary commentary. Record unresolved conflicts
for the Limitations section.

### 5. OUTLINE REFINEMENT
Re-order or add sections if evidence revealed something more important than the plan assumed.
Do not add sections you have no evidence for.

### 6. SYNTHESIZE
Prose first (>= 80% paragraphs, bullets only for genuine lists). Each finding: what the evidence says,
how sources agree/disagree, what it implies. Cite in the same sentence as the claim.

### 7. WRITE `$RUN_DIR/report.md` — **in sections, never in one call**
Create the file with the title + Executive Summary first, then append each remaining section with a separate
`file_operations` call (or `cat >> "$RUN_DIR/report.md" <<'EOF' ... EOF` via shell). One section per tool call keeps
every turn far below the model output limit. Use `references/report_template.md`. Required headings (exact English words, Chinese may follow after `|`):
`## Executive Summary`, `## Introduction`, `## Main Analysis` (with `### Finding N` subsections),
`## Synthesis`, `## Limitations`, `## Recommendations`, `## Bibliography`, `## Methodology`.
Bibliography = one line per source `[N] Title. Year. URL`, numbering contiguous, every cited N present.
Write the language of the report in the language of the request unless told otherwise.

### 8. VALIDATE
```bash
python3 "$SKILL_DIR/validate_report.py" --report "$RUN_DIR/report.md"
```
Fix every ERROR (missing sections, citation/bibliography mismatch, placeholders) and re-run until it passes.
Warnings are acceptable. Then emit the `DR_STATUS` line.

## Quality gates
- quick: >= 6 sources; standard: >= 12 sources; deep: >= 20 sources.
- No placeholders ("TBD", "to be added", "[citation needed]").
- Executive summary 150–400 words. Report length per task prompt.
- Dates: always state the date of the information ("as of <month year>") for fast-moving topics.
