# Intro Manual Keyword Primary Design

## Goal

When a user enters a manual keyword on an Intro job row, that manual keyword should become the primary keyword for that row. The result should keep the keyword source label as `manual`.

## Current Behavior

The Intro backend currently merges GSC queries, DataForSEO ranked keywords, and manual keyword seeds into one scored pool. Manual seeds can win, but they are not guaranteed to win because GSC or DataForSEO candidates may score higher.

The frontend sends each row's `keyword` field as a comma-separated string. The backend parses that string into `manual_seeds`.

## Desired Behavior

If a row contains one or more valid manual keyword seeds:

- The first valid manual seed becomes the primary keyword.
- The primary keyword source label remains `manual`.
- Additional manual seeds may be used as supporting keywords.
- GSC and DataForSEO candidates may still be used as supporting keywords and runner-up candidates.
- H1 fallback only runs when no manual, GSC, or DataForSEO keyword can be selected.

Manual keywords should still respect existing branded/excluded-term filtering. If the first manual seed is filtered out, the backend should try the next valid manual seed.

Manual primary selection should intentionally override the per-job `used_primaries` deduplication rule. If the user enters the same manual keyword on multiple rows, the app should use it on each of those rows because the user made an explicit row-level choice.

## Architecture

Keep the change inside `intro-saas-backend/routers/intro.py`, centered on `select_intro_keywords`. Do not add frontend settings or new API fields.

The selector should still build a merged candidate pool so supporting keyword behavior stays useful. The only changed decision is primary selection: valid manual candidates are checked first, before the ranked score sort chooses a primary from GSC/DFS candidates.

## Data Flow

1. Frontend sends `rows[].keyword` unchanged.
2. Backend splits the row keyword string into `manual_seeds`.
3. Backend gathers GSC queries and DataForSEO ranked keywords as before.
4. Backend enriches manual seeds with DataForSEO volume/difficulty where available.
5. Selector chooses the first valid manual candidate as primary when present.
6. Selector returns supporting keywords from the remaining ranked pool.
7. Result row reports `keyword_source` as `manual`.

## Error Handling

If DataForSEO volume/difficulty lookup fails for manual keywords, manual primary selection still works with default volume/difficulty values. Existing DataForSEO error labels remain unchanged.

If all manual seeds are filtered because they match branded/excluded terms, selection falls back to the existing GSC/DataForSEO scoring behavior.

## Testing

Add focused backend tests for:

- Manual keyword beats a higher-scoring GSC candidate.
- Manual keyword beats a higher-scoring DataForSEO ranked candidate.
- Result label remains `manual` when manual primary is selected.
- Extra manual seeds can appear in supporting keywords.
- Branded/excluded manual seeds are skipped before selecting the next valid source.

Run the Intro backend test suite after implementation.

## Out Of Scope

- No frontend label changes.
- No new "force keyword" toggle.
- No changes to FAQ, Meta, AIO, Schema, Indexer, or Page Copy.
- No change to Google OAuth or service account behavior.
