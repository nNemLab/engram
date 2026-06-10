# syntax=docker/dockerfile:1
FROM python:3.11-slim AS build
ENV UV_LINK_MODE=copy HF_HOME=/opt/models
RUN pip install --no-cache-dir uv
WORKDIR /src
COPY . /src
RUN uv venv /opt/venv && uv pip install --python /opt/venv/bin/python .
# Bake the embedding + reranker models so first run is offline.
RUN /opt/venv/bin/python - <<'PY'
from sentence_transformers import SentenceTransformer, CrossEncoder
SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
PY

FROM python:3.11-slim AS runtime
# Models are baked above; force offline at runtime so a cache miss can't hang on
# the network (the workstation firewall denies container egress by default).
ENV HF_HOME=/opt/models \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    ENGRAM_CONFIG=/data/config.yml \
    PATH=/opt/venv/bin:$PATH
# gosu lets the entrypoint start as root (to fix bind-mount ownership) then drop privileges.
RUN apt-get update && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 engram && mkdir -p /data && chown engram:engram /data
COPY --from=build /opt/venv /opt/venv
COPY --from=build /opt/models /opt/models
# Register the 'engram' Jupyter kernel the seeded playbooks target (papermill resolves it
# from the notebook kernelspec; without it digest/playbook.run fail with NoSuchKernel). Done
# in the runtime stage with --sys-prefix so it lands in the copied /opt/venv/share/jupyter.
RUN /opt/venv/bin/python -m ipykernel install --sys-prefix --name engram --display-name Engram
COPY docker/config.docker.yml /opt/engram/config.docker.yml
COPY docker/entrypoint.sh /opt/engram/entrypoint.sh
COPY scripts/seed_starter_playbooks.py /opt/engram/seed_starter_playbooks.py
COPY vault-template /opt/engram/vault-template
RUN chmod +x /opt/engram/entrypoint.sh
# Entrypoint starts as root to chown the data volume + bind-mounted vault, then
# drops to the engram user (uid 1000 / PUID) via gosu. Do NOT set USER here.
WORKDIR /data
EXPOSE 8765
ENTRYPOINT ["/opt/engram/entrypoint.sh"]
