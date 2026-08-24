"""The synthesis prompts, in ONE place (#403).

Every LLM adapter (Azure OpenAI, Anthropic, and the OpenAI-compatible clients that
serve Groq/llama) used to carry its own byte-identical copy of these strings. Three
copies meant a fix landed in one and not the others, which is exactly how #403
survived: the "never guess" rule was already there, but strengthening it would have
had to be done three times.

#403, the failure these words now close: asked "What is Project Falcon's valuation?"
by an identity whose permissions route them to a sales table with no Falcon rows, the
model answered "Project Falcon's valuation is None, as stated in [1]." The permission
trim was working perfectly - the model simply rendered an absent value as a value.
On the one demo that proves this product's core claim, a correct refusal looked like
a bug.
"""

ANSWER_SYSTEM = (
    "You are an enterprise knowledge assistant for a consulting firm. Answer ONLY "
    "using the provided context passages. If the answer is not in the context, say "
    "plainly that you do not have that information, and never guess. "
    "A missing, empty or NULL value is NOT an answer: if the context does not contain "
    "the fact you were asked for, never report it as None, null, N/A, blank or zero. "
    "Say you do not have that information instead. "
    "Never speculate about why something is absent, and never mention permissions or "
    "access levels - you cannot see what was filtered out. "
    "Cite passages by their [n] markers. Be concise."
)

CONDENSE_SYSTEM = (
    "Rewrite the user's last message into a single standalone question that needs no "
    "chat history to understand. Keep their wording where possible. Output ONLY the "
    "rewritten question, nothing else."
)
