# Hybrid search

Vector search alone misses exact terms — product codes, room numbers, "Sagar
Ginza". Lexical search alone misses paraphrase — "can I bring my dog" against a
document that says "pets are not permitted". Running both and fusing them is not
an optimization; it is what makes the retrieval usable.

Both arms run as CTEs in **one SQL statement**, so there is one round trip and
both see exactly the same tenant predicates.

---

## The query

```sql
WITH vec_raw AS (                       -- semantic arm
    SELECT c.id, 1 - (c.embedding <=> CAST(CAST(:qvec AS text) AS vector)) AS score
    FROM chunks c
    WHERE c.organization_id = :organization_id
      AND c.is_active
      AND (c.location_id = :location_id OR c.location_id IS NULL)
      AND c.embedding IS NOT NULL
    ORDER BY c.embedding <=> CAST(CAST(:qvec AS text) AS vector)
    LIMIT :candidate_k
),
vec AS (SELECT chunk_id, score, row_number() OVER (ORDER BY score DESC) AS rank FROM vec_raw),

tsq AS (                                -- see "AND is the wrong default" below
    SELECT CASE WHEN strpos(raw::text, '!') > 0 THEN raw
                ELSE replace(raw::text, ' & ', ' | ')::tsquery END AS query
    FROM (SELECT websearch_to_tsquery(CAST(:ts_config AS regconfig), :q) AS raw) parsed
),
kw_raw AS (                             -- lexical arm
    SELECT c.id, ts_rank_cd(c.search_vector, tsq.query, 32) AS score
    FROM chunks c CROSS JOIN tsq
    WHERE <the same predicates> AND c.search_vector @@ tsq.query
    ORDER BY score DESC
    LIMIT :candidate_k
),
kw AS (SELECT chunk_id, score, row_number() OVER (ORDER BY score DESC) AS rank FROM kw_raw),

fused AS (SELECT chunk_id, v.score, v.rank, k.score, k.rank
          FROM vec v FULL OUTER JOIN kw k USING (chunk_id))

SELECT …, <fusion expression> AS fused_score
FROM fused f JOIN chunks c ON c.id = f.chunk_id
ORDER BY fused_score DESC
LIMIT :top_k
```

The vector is bound as **text** and cast twice. `CAST(:v AS vector)` alone makes
asyncpg infer the parameter type as `vector`, which then requires pgvector's
codec to be registered on that particular connection. Going through `text` pins
the type to something asyncpg always knows how to send, so the query works
whether or not registration happened.

---

## AND is the wrong default

`websearch_to_tsquery('english', 'breakfast dining room hours')` produces

```
'breakfast' & 'dine' & 'room' & 'hour'
```

— every stem must appear in the same chunk. Real questions rarely satisfy that.
A chunk reading *"Breakfast is served from 7:00 AM to 10:00 AM in the main dining
room"* does not contain "hours", so it does not match, and the lexical arm
returns **nothing**.

The failure is invisible: the vector arm still returns results, the endpoint
still answers, and "hybrid" search has quietly become vector-only. It was caught
here by an integration test asserting that both arms contribute
([`test_both_arms_contribute`](../tests/integration/test_retrieval.py)) — not by
anything user-facing.

So the parsed query's conjunctions are relaxed to disjunctions, leaving
`ts_rank_cd` to do the discriminating, which is what it is for. Phrase operators
(`<->`) survive untouched, so quoted phrases still behave. A query containing
negation is left alone, because relaxing `a & !b` to `a | !b` matches nearly
everything.

Set `RETRIEVAL__LEXICAL_RELAX_TO_OR=false` for keyword-shaped queries where
precision matters more than recall.

---

## Fusion

**Reciprocal Rank Fusion is the default.**

```
score(d) = Σ  weight_arm / (k + rank_arm(d))          k = 60
```

It consumes ranks, not scores, which makes it immune to the failure that makes
naive weighted-sum fusion unreliable: when one arm returns a tight cluster of
near-identical scores — very common for cosine similarity over a small tenant —
min-max normalization stretches meaningless differences across the full 0–1 range
and that arm dominates. RRF cannot do that. It also degrades gracefully when one
arm returns nothing at all, with no special case.

**`WeightedScoreFusion`** is available via `RETRIEVAL__FUSION=weighted`:
per-arm min-max normalization, then `vector_weight · v + keyword_weight · k`.
Use it when score magnitudes genuinely carry signal for your corpus and you want
to tune the balance directly.

Both are expressed as a SQL fragment plus bind parameters, so fusion happens in
the same round trip; and both are also pure Python functions, used for merging
across multiple query rewrites where there is no SQL to put them in.

---

## Filtered-ANN recall

This is the subtle one.

An HNSW index traverses the graph globally and the tenant predicate is applied to
what comes back. A small tenant inside a large corpus can get two results for a
query that would return fifty unfiltered — the traversal spent its budget on
other tenants' rows and discarded them.

Four mitigations, all in `postgres_hybrid.py`:

1. **The HNSW index is partial on `is_active`.** The graph then holds only rows a
   query could return — no superseded versions, no chunks from a version still
   being ingested.
2. **`hnsw.iterative_scan = relaxed_order`** lets pgvector keep scanning until it
   has enough surviving rows. Detected from `extversion`, not assumed: it exists
   only in pgvector ≥ 0.8.
3. **`ef_search` scales with `candidate_k`**, and the query is retried once at
   `ef_search × 3` when the vector arm under-returns.
4. **The lexical arm is a floor.** GIN pre-filters correctly, so keyword matches
   are never lost to this effect.

A retry sets `degraded: true` on the result and is logged as
`search_recall_retry`, rather than being silently absorbed — silently degraded
recall is exactly the kind of problem that goes unnoticed for months.

Past roughly 2M chunks per tenant, partition `chunks` by `organization_id`; the
predicate becomes partition elimination and the problem disappears.

---

## Per-language text search

`language → regconfig` is mapped in one place (`TS_CONFIG_BY_LANGUAGE`) and used
by **both** the indexing stage and the search query. Indexing with `english` and
querying with `simple` produces an index that matches nothing, which looks
exactly like "retrieval got worse".

Languages PostgreSQL has no stemmer for fall back to `simple`, which still
indexes them — for CJK that is the correct behaviour anyway.

The lexical vector is built in the INSERT rather than by a generated column,
because `to_tsvector` with a configuration chosen from a column is not immutable
and generated columns require immutability. Headings are weighted `A`, body `B`:

```sql
setweight(to_tsvector(cfg, heading), 'A') || setweight(to_tsvector(cfg, content), 'B')
```

---

## Tuning

| Setting | Default | Effect |
|---|---|---|
| `RETRIEVAL__CANDIDATE_K` | 50 | Rows each arm returns before fusion. Raise for recall, at latency cost. |
| `RETRIEVAL__TOP_K` | 8 | Passages reaching the context builder. |
| `RETRIEVAL__RRF_K` | 60 | Higher flattens the head of the distribution. |
| `RETRIEVAL__VECTOR_WEIGHT` / `KEYWORD_WEIGHT` | 0.6 / 0.4 | Arm balance. |
| `RETRIEVAL__EF_SEARCH` | 200 | HNSW probe width. |
| `RETRIEVAL__LOCATION_BOOST` | 1.02 | See below. |

### Why the location boost is 1.02

It is derived, not guessed. With RRF at `k=60`, a hit at rank *r* scores
`1/(60+r)`, so a boost *b* lifts rank *r* above rank 1 exactly when
`b > (60+r)/61`:

| boost | highest rank that can overtake rank 1 |
|---|---|
| 1.02 | 2 |
| 1.05 | 4 |
| 1.25 | 15 |

At 1.25 an *irrelevant* location passage buries relevant organization content —
which is what happened in the first run of the demo, where Ginza's "Fitness
Centre" outranked the group's breakfast policy for a breakfast query.

The right value depends on the fusion strategy's score spread: weighted fusion
spans 0–1 and needs a larger boost for the same effect.

Preference is not the override mechanism. [Suppression](versioning.md) is; the
boost only wins ties.
