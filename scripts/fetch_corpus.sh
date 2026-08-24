#!/usr/bin/env bash
# Populate sample_corpus/ with trustworthy PUBLIC documents for the RAG eval (#42/#46).
# These are not committed (gitignored, large binaries); this script makes them reproducible.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p sample_corpus
cd sample_corpus

dl() { [ -f "$2" ] && echo "  have $2" || { echo "  fetch $2"; curl -fsSL "$1" -o "$2"; }; }

echo "Fetching demo corpus into sample_corpus/ …"
# arXiv papers (RAG / Transformers canon)
dl "https://arxiv.org/pdf/1706.03762"  "attention-is-all-you-need.pdf"   # Vaswani et al., Transformer
dl "https://arxiv.org/pdf/1810.04805"  "bert.pdf"                        # Devlin et al., BERT
dl "https://arxiv.org/pdf/2005.11401"  "rag-lewis-2020.pdf"              # Lewis et al., RAG
# NIST frameworks (governance / risk — consulting-relevant)
dl "https://nvlpubs.nist.gov/nistpubs/CSWP/NIST.CSWP.29.pdf"   "nist-csf-2.0.pdf"      # Cybersecurity Framework 2.0
dl "https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf"    "nist-ai-rmf-100-1.pdf" # AI Risk Management Framework
# Project Gutenberg (messy long-form prose)
dl "https://www.gutenberg.org/files/1342/1342-0.txt"  "pride-and-prejudice.txt"   # Austen
dl "https://www.gutenberg.org/files/1661/1661-0.txt"  "sherlock-holmes.txt"       # Doyle

echo "Done. $(ls -1 | wc -l | tr -d ' ') files in sample_corpus/."
