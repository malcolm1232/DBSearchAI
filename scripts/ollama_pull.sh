#!/usr/bin/env bash
# Pull the local models the self-host edition uses. OPTIONAL: `docker compose up` now
# auto-pulls these via the ollama-init service. Use this only for a manual/offline pull.
# These run entirely inside the ollama container — nothing leaves your machine.
set -e
docker compose exec ollama ollama pull nomic-embed-text   # embeddings (768-dim)
docker compose exec ollama ollama pull llama3.2            # answer generation
echo "Models ready. Try: curl localhost:8080/health"
