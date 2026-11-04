# OEVRYN public data

Independent GitHub Pages publication of the complete modern paper and GitHub/news datasets for [OEVRYN Engine](https://yazhouzhu19.github.io/research-monitor/).

The hourly workflow reads verified public source archives from `YazhouZhu19/YazhouZhu19.github.io`, checks every field and shard before deployment, preserves the current and preceding generation, and verifies the public revision and manifest hashes. It uses only this repository's GitHub Actions token; no personal access token or cross-repository write permission is required.

Source collection and web publication remain independent. Failed data builds do not replace the last successful Pages deployment. `deployment.json` records source revision, collection times and record counts. Legacy URLs on the web site remain a dated transition snapshot; this site supplies current data to the new client.

`data/paper-overview.json` is a validated, lightweight dashboard projection produced by newer source revisions. It preserves overview filters, work grouping and local watchlist matching. Full abstracts and collection evidence remain in the complete paper snapshots; do not treat the projection as complete AI evidence. The deployment verifies the overview hash when that format is present.
