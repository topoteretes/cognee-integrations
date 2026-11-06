# Evaluation fixtures

`supplier_tasks.json` lists task variations with the deterministic answer a
controlled fixture site would give. Serve your own copy of such a site (two
static pages are enough) and point the tasks at it; never evaluate against a
live third-party site whose content can change between conditions.

`../run_comparison.py --fake` exercises the harness without a browser or LLM.
Results written to `../results/` are harness output and are not committed.
