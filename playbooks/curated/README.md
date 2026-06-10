# Playbooks — curated (Marimo)

The **graduated tier**. Workflows that have proven their value and are now relied
upon. Stored as Marimo `.py` files for clean diffs and reproducibility.

- Runtime: `marimo run <file>.py --headless`.
- Parameterization: `mo.cli_args()` inside the notebook → CLI flags.
- Source format: pure Python. Edit in any text editor; the agent can read/modify.

Conventions:
- Lock dependencies (pin versions in a comment block at the top of the file).
- Each curated playbook should have a top-level docstring describing intent and inputs.
- If a curated playbook breaks, fix it — they're under SLA. Scratch playbooks are not.
