# OpenLigaDB adapter preparation

## Status

The optional adapter in `openligadb_provider.py` is isolated and does not replace SportScore, write to Neon, or run during app startup. It normalizes fixture identity, kickoff, full-time score and half-time score fields.

## Why it is not enabled in production yet

- OpenLigaDB coverage is community-maintained and varies by league and season. Verify the exact competition/season before importing.
- The API does not provide the detailed corner, card, shots, odds, injuries or lineup data required by all SRC Match Analyst markets.
- Missing values must remain NULL/unknown; never convert blanks to zero.
- Existing SportScore rows and history must remain untouched during any future import.
- The public app must retrieve data centrally and serve cached responses. Do not make one OpenLigaDB request per end user.
- Use a meaningful `OPENLIGADB_USER_AGENT` environment value with an application contact route/address.
- Follow the API fair-use terms: poll `getlastchangedate`, fetch full match data only after changes, cache results, use exponential backoff for 429/5xx, and keep live polling to the provider's stated interval.
- Public displays must show: `Data: OpenLigaDB (www.openligadb.de), licensed under ODbL 1.0`.

## Safe rollout plan

1. Check available leagues and seasons using `available_leagues()`.
2. Explicitly map only verified league shortcuts and season identifiers.
3. Compare provider fixture IDs, teams, kickoff and scores against existing rows before import.
4. Import only records with a stable external ID; retain source attribution and provider identity.
5. Do not overwrite SportScore records or existing user predictions.
6. Validate sample counts, missing half-time scores and duplicate rates in a staging database.
7. Enable only after the app's public multi-user authentication and per-user saved-match authorization are implemented.

## Current decision

SportScore remains the active provider. This adapter is a preparation step only; it is not a production data source and has not been deployed.
