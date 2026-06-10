# Playbooks — scratch (Jupyter)

The **low-ceremony tier**. Drop `.ipynb` files here for fast exploration.

- Runtime: Jupyter via Papermill (headless).
- Parameterization: a cell tagged `parameters` (Papermill convention).
- Outputs: full executed notebook lands in `playbooks/runs/<run_id>/notebook.ipynb`.

Promotion path: when a playbook proves its worth and you want it to run reliably,
rewrite it as Marimo and move it to `../curated/`. That's the deliberate act of
saying "this one matters."
