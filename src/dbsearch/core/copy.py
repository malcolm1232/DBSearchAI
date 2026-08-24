"""User-facing sentences that more than one component has to say identically.

Kept here because the alternative already failed: the no-evidence sentence below was
copy-pasted into six adapter methods and the router, and every copy asserted a CAUSE that
the component saying it could not possibly know (#393).

Rule for anything added here: a component may only state what it can observe. A synthesizer
observes "no evidence was retrieved for this question". It does not observe whether the
corpus is empty, whether the caller is entitled to nothing, or whether retrieval simply
missed - those are three different situations with three different remedies, and the surface
decides between them from the corpus status that ships with the answer.
"""

# Said when retrieval returned no evidence. Deliberately silent about WHY.
#
# It used to read "I couldn't find anything you have access to about that.", which named
# permissions as the cause on every no-evidence path - including a completely empty index.
# An operator hit exactly that on prod, read it as "I am not allowed to see anything", and
# went looking for an access bug that did not exist (#392). The honest denominator now rides
# alongside the answer, so this line no longer has to guess.
#
# LAW 2 note: this must stay generic. On the router's no-visible-stores path it is also what
# stops an unauthorized caller learning that a store they cannot see exists at all - saying
# less here is a security property, not just good manners.
NO_EVIDENCE_ANSWER = "I couldn't find anything matching that."
