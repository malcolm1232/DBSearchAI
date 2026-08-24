# Retained run artifacts

`eval_results/runs/` is gitignored by design - it is a scratch directory, and a full
sweep writes a lot into it.

That bit us once: the semantic probe this research is built on lived only there, and was
deleted along with a worktree. The prose survived, the evidence did not.

So any run a notebook CITES gets copied here, where it is tracked. The rule is simple:
if a number appears in `research/retrieval/`, the artifact behind it lives in this folder.
