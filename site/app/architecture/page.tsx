import Link from "next/link";
import type { Metadata } from "next";
import {
  ArrowDown,
  ArrowRight,
  Check,
  Lock,
  Server,
  ShieldCheck,
  ShieldOff,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { SectionHeading } from "@/components/sections/section-heading";
import { CtaBand } from "@/components/sections/cta-band";
import { ArchToc } from "@/components/arch-toc";
import { ChapterTabs } from "@/components/chapter-tabs";
import { START_URL } from "@/lib/nav";
import { FileLink } from "@/components/file-link";

export const metadata: Metadata = {
  title: "Architecture - DBSearch.AI",
  description:
    "The two laws of the codebase, end to end: data residency (a data plane in your tenant, a boundary validator at the single door out) and permission-faithful retrieval (four independent layers between a user and a document they aren't allowed to see) - quoted straight from the codebase.",
};

/*
 * Every snippet below is quoted VERBATIM from the codebase (including the
 * original comments), so that when the repository goes public these blocks
 * can become links to source without a single character changing. Do not
 * paraphrase them to fit house copy style.
 */
const SNIPPET_EMIT = `ts = self._now()
self.agent.emit(
    "ingest.completed",
    counts={"docs_indexed": 1, "chunks_created": 1 if text.strip() else 0},
    health={"index_ready": True, "last_index_ts": ts},
    ts=ts,
)`;

const SNIPPET_SCHEMA = `"additionalProperties": false,
"required": ["tenant_id", "event", "ts"],
"properties": {
  "tenant_id": { "type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$" },
  "event": { "type": "string", "enum": ["ingest.completed", "query.served", ...] },
  "counts": { "description": "Integer counters only — never identifiers or content." },
  "cost": { "description": "Usage/cost meters for billing (LAW 8)." },
  "health": { "additionalProperties": { "type": ["number", "string", "boolean"] } },
  "ts": { "type": "string", "description": "ISO-8601 timestamp." }
},
"x-forbidden-keys": [
  "text", "content", "body", "snippet", "chunk", "title", "query",
  "filename", "file_name", "answer", "document", "doc", "email", "name",
  "uri", "url", "path", "user", "embedding"
]`;

/* Composed exhibit: the two verbatim validate calls with location annotations,
   showing the same guard runs on both sides of the wire. */
const SNIPPET_VALIDATE_TWICE = `# in DataPlaneAgent.emit(), before anything is sent:
self._validator.validate(payload)  # local guard — raises before anything crosses

# in ControlPlane.receive_telemetry(), on arrival:
self._validator.validate(payload)  # re-validate; never trust the network`;

const SNIPPET_DENYLIST = `def _reject_forbidden_keys(self, obj: object, path: str = "") -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.lower() in self.forbidden:
                raise BoundaryViolation(
                    f"content-bearing key '{path}{key}' may not cross the uplink (LAW 1)"
                )
            self._reject_forbidden_keys(value, f"{path}{key}.")`;

const SNIPPET_SEND = `self._validator.validate(payload)  # local guard — raises before anything crosses
if self.air_gapped:
    return False                    # uplink severed; data plane still fully functional
self._cp.receive_telemetry(payload)
return True`;

const SNIPPET_RECEIVE = `def receive_telemetry(self, payload: dict) -> bool:
    try:
        self._validator.validate(payload)
    except BoundaryViolation as e:
        # Record the REASON only. The offending payload never enters our systems (LAW 1).
        self.violations.append({"tenant_id": payload.get("tenant_id", "?"), "reason": str(e)})
        raise`;

const SNIPPET_PAYLOAD = `{
  "tenant_id": "acme-001",
  "event": "ingest.completed",
  "counts": { "docs_indexed": 12000, "errors": 1 },
  "cost": { "embed_tokens": 1200000, "llm_tokens": 90000 },
  "health": { "index_pct": 0.94, "queue_depth": 12, "version": "2.3.1" },
  "ts": "2026-06-25T10:00:00Z"
}`;

const SNIPPET_AIRGAP = `DataPlaneAgent(tenant_id, control_plane, air_gapped=True)`;

const SNIPPET_STAMP = `chunk = Chunk(
    ...
    allowed_principals=[p.oid for p in doc.acl],  # denormalized ACL (LAW 2)
    ...
)`;

const SNIPPET_SEARCH_SIG = `@abstractmethod
def search(self, query_embedding: list[float], principals: list[str], top_k: int) -> list[dict]:
    """\`principals\` is a MANDATORY filter applied here — callers cannot omit it."""`;

const SNIPPET_RETRIEVE = `principals = self._identity.expand_groups(user_oid)        # (1)
query_vec = self._embedder.embed([question])[0]            # (2)
candidate_k = self._top_k * self._rerank_factor if self._rerank else self._top_k
hits = self._index.search(query_vec, principals, candidate_k)  # (3) mandatory trim`;

const SNIPPET_IMPERSONATE = `# 1. IMPERSONATION PREVENTED: authenticated as bob, but body claims user=alice.
#    The body identity must be ignored — bob is not on deal-team, so no deal doc.
r = client.post("/search", headers={"X-DBSearch-User": "bob"}, json={"user": "alice", "question": q})
assert DEAL not in r.json()["authorized_docs"], "IMPERSONATION: bob got alice's doc via body field"`;

const SNIPPET_FALCON = `assert FALCON in a.authorized_docs, "alice should retrieve the Falcon doc"
assert any(c["doc"] == FALCON for c in a.citations), "alice's answer should cite Falcon"

assert FALCON not in b.authorized_docs, "LAW 2 BREACH: bob retrieved the Falcon doc"
assert all(c["doc"] != FALCON for c in b.citations), "LAW 2 BREACH: bob's citations leak Falcon"`;

const SNIPPET_INTERSECT = `chunk.allowed_principals    ["grp-directors"]
bob's principals            ["bob", "grp-sales"]
intersection                ∅   ->  the index never returns the chunk`;

const DATA_PLANE_HOLDS = [
  "Documents",
  "File bytes",
  "Chunks",
  "Embeddings",
  "Vector index",
  "LLM inference",
  "Queries",
  "Answers",
] as const;

const CONTROL_PLANE_HOLDS = [
  "Health dashboards",
  "Billing meters",
  "Release pushes",
] as const;

const NEVER_CROSSES = [
  "Document text or chunks",
  "File names and titles",
  "The user's question",
  "The answer shown to the user",
  "Embeddings, URLs, user names",
] as const;

const SMUGGLE_TESTS = [
  "A document snippet in the payload",
  "The user's query string",
  "A field that isn't in the contract",
  "A filename nested deep inside counts",
  "A made-up event name outside the enum",
] as const;

const VAULT_WRONG = [
  "Bob asks a question",
  "Open every deposit box",
  "Pick the ones Bob owns",
  "Show Bob only his",
] as const;

const VAULT_RIGHT = [
  "Bob asks a question",
  "Ask the vault which boxes are Bob's",
  "Open only those boxes",
] as const;

/* Anchor ids double as the scroll-rail targets. Laws 1 and 2 point at their
   deep-dive sections; 3-8 point at their card in the at-a-glance grid. */
const LAWS_INDEX = [
  { id: "law-1", num: "Law 1", name: "Data residency" },
  { id: "law-2", num: "Law 2", name: "Permission-faithful" },
  { id: "law-3", num: "Law 3", name: "Connectors" },
  { id: "law-4", num: "Law 4", name: "Async pipeline" },
  { id: "law-5", num: "Law 5", name: "Tenant isolation" },
  { id: "law-6", num: "Law 6", name: "Stateless compute" },
  { id: "law-7", num: "Law 7", name: "Cloud-portable" },
  { id: "law-8", num: "Law 8", name: "Observability" },
] as const;

/* Rail data: laws are always-visible rows; the deep-dive laws carry the
   subsection wheel. Subsections use the sections' short kicker tags - full
   titles would overflow the rail. */
const ARCH_TOC = [
  {
    id: "law-1",
    label: "Law 1",
    children: [
      { id: "law-1", label: "The shape" },
      { id: "law-1-flow", label: "The flow" },
      { id: "law-1-wire", label: "The wire" },
      { id: "law-1-controls", label: "Your controls" },
      { id: "law-1-tests", label: "Verification" },
    ],
  },
  {
    id: "law-2",
    label: "Law 2",
    children: [
      { id: "law-2", label: "The setup" },
      { id: "law-2-layers", label: "Four layers" },
      { id: "law-2-tests", label: "Verification" },
    ],
  },
  { id: "law-3", label: "Law 3" },
  { id: "law-4", label: "Law 4" },
  { id: "law-5", label: "Law 5" },
  { id: "law-6", label: "Law 6" },
  { id: "law-7", label: "Law 7" },
  { id: "law-8", label: "Law 8" },
] as const;

const OTHER_LAWS = [
  {
    id: "law-3",
    num: "Law 3",
    title: "Isolated, resumable connectors",
    body: "Each data source is its own module with its own auth, sync cursor and health. A failing source flips only itself to error - it never corrupts the index or blocks another source - and every write is idempotent, so a retry replaces rather than duplicates.",
  },
  {
    id: "law-4",
    num: "Law 4",
    title: "Async, queue-driven pipeline",
    body: "Parse, chunk-and-embed, and index are separate stages joined by durable queues, and the messages carry references, never bytes. Each stage scales on its own bottleneck, and a crashed worker just means its message is retried by a sibling.",
  },
  {
    id: "law-5",
    num: "Law 5",
    title: "Isolation by construction",
    body: "Customers don't share an index with a filter keeping them apart - each deployment is its own stack in the customer's own subscription. As defense in depth, the tenant id is also threaded through every record, storage key and metering bucket.",
  },
  {
    id: "law-6",
    num: "Law 6",
    title: "Stateless compute",
    body: "Workers and the query API remember nothing between requests; all durable state lives in managed stores. Any worker can die mid-job and be replaced without losing anything, which is what makes scale-out and zero-downtime deploys possible.",
  },
  {
    id: "law-7",
    num: "Law 7",
    title: "Cloud-portable by ports and adapters",
    body: "Every cloud-specific capability sits behind an internal interface; the core imports no cloud SDK at all. Swapping the vector database, the model, or the whole cloud is configuration - including bringing your own in-tenant model, or self-hosting the identical core.",
  },
  {
    id: "law-8",
    num: "Law 8",
    title: "Observability without content",
    body: "Every operation emits counters, cost meters and health - and that telemetry rides the same boundary validator as law one. A metric is provably a number, so we can answer 'is this customer healthy, what did they cost' without the ability to read anything.",
  },
] as const;

const OWNED_CONTROLS = [
  {
    icon: Server,
    title: "Private endpoints",
    body: "Every Azure service the data plane touches - AI Search, Blob, Azure OpenAI, Key Vault - is reached over private endpoints inside your VNet. Traffic never touches the public internet.",
  },
  {
    icon: Lock,
    title: "Deny-all outbound",
    body: "Your NSG or Azure Firewall denies all outbound traffic, with an explicit allow-list of at most one destination: the telemetry endpoint, over mutual TLS. Regular TLS authenticates only the server; in mutual TLS both sides present certificates, so the client must prove who it is before a connection even exists.",
  },
  {
    icon: ShieldOff,
    title: "Air-gapped mode",
    body: "For a customer who won't allow even telemetry to leave (eg. a defense or intelligence client) - there's a single flag:",
    chip: "air_gapped=True",
    body2:
      "When that's on, emit() validates as normal but then just returns False - the uplink is severed, nothing is sent at all. The data plane keeps working perfectly; you just lose the remote telemetry. Crucially it's the same codebase - air-gap is a config flag, not a separate fork.",
  },
] as const;

function CodeRef({
  file,
  code,
  note,
}: {
  file: string;
  code: string;
  note?: string;
}) {
  return (
    // min-w-0: inside a flex/grid item the pre's min-content width would
    // otherwise widen the column past the viewport instead of scrolling
    <figure className="min-w-0 max-w-full overflow-hidden rounded-lg border border-border bg-surface">
      <figcaption className="border-b border-border px-4 py-2.5 font-mono text-xs text-fg">
        <FileLink file={file} />
      </figcaption>
      <pre className="overflow-x-auto p-4 font-mono text-[13px] leading-relaxed text-fg-muted">
        <code>{code}</code>
      </pre>
      {note && (
        <p className="border-t border-border px-4 py-2.5 text-xs text-fg-muted">
          {note}
        </p>
      )}
    </figure>
  );
}

function VaultChain({ steps }: { steps: readonly string[] }) {
  return (
    <ol className="mt-4 flex flex-wrap items-center gap-y-2">
      {steps.map((step, i) => (
        <li key={step} className="flex items-center">
          {i > 0 && (
            <ArrowRight
              className="mx-1.5 h-3.5 w-3.5 shrink-0 text-fg-muted"
              aria-hidden="true"
            />
          )}
          <span className="rounded-md bg-surface-muted px-2.5 py-1 font-mono text-xs text-fg">
            {step}
          </span>
        </li>
      ))}
    </ol>
  );
}

function PlaneConnector({ label }: { label: string }) {
  return (
    <div className="flex flex-col items-center gap-1 py-1" aria-hidden="true">
      <span className="h-4 w-px bg-border" />
      <span className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-widest text-fg-muted">
        <ArrowDown className="h-3.5 w-3.5" />
        {label}
      </span>
      <span className="h-4 w-px bg-border" />
    </div>
  );
}

export default function ArchitecturePage() {
  return (
    <main>
      <ArchToc items={ARCH_TOC} />
      {/* Hero */}
      <section className="vault-grid border-b border-border">
        <div className="mx-auto max-w-3xl px-6 py-20 text-center lg:py-28">
          <p className="text-micro font-mono text-fg-muted">Architecture</p>
          <div className="flex justify-center">
            <ChapterTabs active="laws" />
          </div>
          <h1 className="mt-6 font-display text-display-2 text-fg">
            Your documents never leave your cloud
          </h1>
          <p className="mx-auto mt-6 max-w-2xl text-lg text-fg-muted">
            We call it law one: data residency. Everything that touches your
            actual data - parsing, chunking, embedding, indexing, searching,
            even the AI inference - runs inside your own cloud tenant. The
            only thing that ever travels back to us is telemetry: numbers, a
            boolean, a timestamp. This page walks the two biggest laws in the
            codebase end to end, in the actual code: first data residency,
            then law two, permission-faithful retrieval.
          </p>
          <div className="mt-8 flex flex-wrap items-center justify-center gap-4">
            <Button asChild variant="pill">
              <Link href="/demo">Book a demo</Link>
            </Button>
            <Button asChild variant="quiet">
              <a href={START_URL}>Self-host free</a>
            </Button>
          </div>
          <nav aria-label="The laws" className="mt-10">
            <ul className="flex flex-wrap items-center justify-center gap-2">
              {LAWS_INDEX.map(({ id, num, name }) => (
                <li key={id}>
                  <a
                    href={`#${id}`}
                    className="inline-block rounded-full border border-border px-3 py-1.5 font-mono text-xs text-fg-muted transition-colors hover:border-fg hover:text-fg"
                  >
                    {num} · {name}
                  </a>
                </li>
              ))}
            </ul>
          </nav>
        </div>
      </section>

      {/* The two planes */}
      <section id="law-1" className="mx-auto max-w-6xl scroll-mt-20 px-6 py-24">
        <SectionHeading
          kicker="Law 1 · data residency"
          title="Two planes, one narrow door"
          sub="The product splits into a data plane you host and a control plane we host. Between them sits a single uplink, and a guard that runs on your side of it."
        />

        <div className="mx-auto mt-16 max-w-2xl">
          <div className="rounded-lg border border-border bg-surface p-6 sm:p-8">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <p className="font-mono text-xs font-semibold uppercase tracking-widest text-fg">
                Data plane
              </p>
              <p className="font-mono text-[11px] uppercase tracking-widest text-fg-muted">
                your cloud tenant
              </p>
            </div>
            <ul className="mt-5 flex flex-wrap gap-2">
              {DATA_PLANE_HOLDS.map((item) => (
                <li
                  key={item}
                  className="rounded-md bg-surface-muted px-2.5 py-1 font-mono text-xs text-fg"
                >
                  {item}
                </li>
              ))}
            </ul>
            <p className="mt-5 text-sm text-fg-muted">
              All of it lives and dies in here. Never copied out.
            </p>
          </div>

          <PlaneConnector label="the only door out" />

          <div className="flex items-start gap-4 rounded-lg border border-fg/30 bg-bg p-6">
            <ShieldCheck
              className="mt-0.5 h-5 w-5 shrink-0 text-accent"
              aria-hidden="true"
            />
            <div>
              <p className="font-mono text-xs font-semibold uppercase tracking-widest text-fg">
                Boundary validator
              </p>
              <p className="mt-2 text-sm text-fg-muted">
                Runs inside your tenant, before anything is sent. If a payload
                would carry content, it raises an exception and the send line
                is never reached.
              </p>
            </div>
          </div>

          <PlaneConnector label="metadata only" />

          <div className="rounded-lg border border-border bg-surface p-6 sm:p-8">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <p className="font-mono text-xs font-semibold uppercase tracking-widest text-fg">
                Control plane
              </p>
              <p className="font-mono text-[11px] uppercase tracking-widest text-fg-muted">
                our cloud
              </p>
            </div>
            <ul className="mt-5 flex flex-wrap gap-2">
              {CONTROL_PLANE_HOLDS.map((item) => (
                <li
                  key={item}
                  className="rounded-md bg-surface-muted px-2.5 py-1 font-mono text-xs text-fg"
                >
                  {item}
                </li>
              ))}
            </ul>
            <p className="mt-5 text-sm text-fg-muted">
              Receives counts, costs and timestamps. Never content.
            </p>
          </div>
        </div>
      </section>

      {/* The flow, in code */}
      <section id="law-1-flow" className="scroll-mt-20 border-y border-border bg-surface">
        <div className="mx-auto max-w-6xl px-6 py-24">
          <SectionHeading
            kicker="Law 1 · the flow"
            title="Follow one event through the boundary"
            sub="Say a document finishes ingesting and the system wants to tell us that it happened. Here is everything that occurs, quoted verbatim from the codebase."
          />

          <p className="mx-auto mt-6 max-w-2xl text-center text-sm text-fg-muted">
            We are preparing the full codebase for open source. Until the
            repository is public these references are shown unlinked; once it
            is, every block below links straight to source.
          </p>

          <div className="mx-auto mt-16 flex max-w-3xl flex-col gap-6">
            <div>
              <p className="text-micro font-mono text-fg-muted">Step 1</p>
              <h3 className="mt-2 text-lg font-semibold text-fg">
                A document finishes indexing - the data plane builds its
                report
              </h3>
              <p className="mt-2 text-sm text-fg-muted">
                The system wants to tell us &quot;a document was
                indexed&quot;, so it builds the message: an event name, two
                counters, a boolean and a timestamp. Notice what it does not
                build: no title, no text, no filename.
              </p>
              <div className="mt-4">
                <CodeRef
                  file="src/dbsearch/server/edition.py"
                  code={SNIPPET_EMIT}
                />
              </div>
            </div>

            <PlaneConnector label="the payload heads for the only door out" />

            <div>
              <p className="text-micro font-mono text-fg-muted">Step 2</p>
              <h3 className="mt-2 text-lg font-semibold text-fg">
                At the door, the guard checks it two ways
              </h3>
              <p className="mt-2 text-sm text-fg-muted">
                First, an{" "}
                <strong className="font-semibold text-fg">
                  allow-list: the schema is the contract.
                </strong>{" "}
                A JSON schema, boundary.schema.json, says the only fields
                allowed to cross are tenant_id, event, counts, cost, health
                and ts. The payload is validated inside DataPlaneAgent before
                it is sent - and the control plane runs the very same
                validation again on arrival.
              </p>
              <div className="mt-4 flex flex-col gap-4">
                <CodeRef
                  file="src/dbsearch/boundary/boundary.schema.json"
                  code={SNIPPET_SCHEMA}
                />
                <CodeRef
                  file="src/dbsearch/controlplane/agent.py:40 · plane.py:41"
                  code={SNIPPET_VALIDATE_TWICE}
                />
              </div>
              <p className="mt-6 text-sm text-fg-muted">
                Second, a{" "}
                <strong className="font-semibold text-fg">
                  deny-list: forbidden words, at any depth.
                </strong>{" "}
                The guard also recursively scans the whole payload for
                content-bearing keys - text, content, snippet, title, query,
                answer, filename, embedding, email, name and more. If any of
                those appears anywhere, even nested deep inside counts, the
                payload is rejected.
              </p>
              <div className="mt-4">
                <CodeRef
                  file="src/dbsearch/boundary/validator.py"
                  code={SNIPPET_DENYLIST}
                />
              </div>
            </div>

            <PlaneConnector label="clean payloads only" />

            <div>
              <p className="text-micro font-mono text-fg-muted">Step 3</p>
              <h3 className="mt-2 text-lg font-semibold text-fg">
                A dirty payload dies here - a clean one reaches the send line
              </h3>
              <p className="mt-2 text-sm text-fg-muted">
                If the guard raised, the send line below it is never executed
                and nothing crosses. An air-gapped tenant stops here
                unconditionally: validated, then dropped - the uplink is
                severed.
              </p>
              <div className="mt-4">
                <CodeRef
                  file="src/dbsearch/controlplane/agent.py"
                  code={SNIPPET_SEND}
                />
              </div>
            </div>

            <PlaneConnector label="crosses the uplink - metadata only" />

            <div>
              <p className="text-micro font-mono text-fg-muted">Step 4</p>
              <h3 className="mt-2 text-lg font-semibold text-fg">
                On arrival, we refuse to trust the network
              </h3>
              <p className="mt-2 text-sm text-fg-muted">
                The control plane runs the exact same contract again on what
                it receives. If a bad payload somehow shows up, only the
                reason is recorded - the payload itself never enters our
                systems.
              </p>
              <div className="mt-4">
                <CodeRef
                  file="src/dbsearch/controlplane/plane.py"
                  code={SNIPPET_RECEIVE}
                />
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* What crosses the wire */}
      <section id="law-1-wire" className="mx-auto max-w-6xl scroll-mt-20 px-6 py-24">
        <SectionHeading
          kicker="Law 1 · the wire"
          title="What a crossing actually looks like"
          sub="This is a real payload from the boundary self-test: the legitimate case that must pass. You can run an entire business on this - is the customer healthy, what did they cost - without the ability to read a single document."
        />

        <div className="mx-auto mt-16 grid max-w-4xl gap-6 lg:grid-cols-[3fr_2fr]">
          <CodeRef file="tests/selftest_boundary.py" code={SNIPPET_PAYLOAD} />
          <div className="rounded-lg border border-destructive/40 bg-surface p-6">
            <p className="flex items-center gap-2 font-mono text-xs font-semibold uppercase tracking-widest text-destructive">
              <X className="h-4 w-4" aria-hidden="true" />
              Never crosses
            </p>
            <ul className="mt-5 space-y-3">
              {NEVER_CROSSES.map((item) => (
                <li
                  key={item}
                  className="flex items-start gap-3 text-sm text-fg"
                >
                  <X
                    className="mt-0.5 h-4 w-4 shrink-0 text-destructive"
                    aria-hidden="true"
                  />
                  {item}
                </li>
              ))}
            </ul>
          </div>
        </div>
      </section>

      {/* The proof you own */}
      <section id="law-1-controls" className="scroll-mt-20 border-y border-border bg-surface">
        <div className="mx-auto max-w-6xl px-6 py-24">
          <SectionHeading
            kicker="Law 1 · your controls"
            title="Don't take our code's word for it"
            sub="The boundary validator is our code. The whole data plane runs in your subscription and the network egress lockdown is yours - and that is the real proof. The data plane is deployed inside your VNet, so anything that could ever leave has to pass your firewall."
          />

          <div className="mt-16 grid gap-6 lg:grid-cols-3">
            {OWNED_CONTROLS.map((item) => (
              <div
                key={item.title}
                className="rounded-lg border border-border bg-bg p-8"
              >
                <item.icon className="h-6 w-6 text-accent" aria-hidden="true" />
                <h3 className="mt-4 text-lg font-semibold text-fg">
                  {item.title}
                </h3>
                <p className="mt-2 text-sm text-fg-muted">{item.body}</p>
                {"chip" in item && (
                  <>
                    <p className="mt-3 max-w-max rounded-md bg-surface-muted px-3 py-2 font-mono text-xs text-fg">
                      {item.chip}
                    </p>
                    <p className="mt-3 text-sm text-fg-muted">{item.body2}</p>
                  </>
                )}
              </div>
            ))}
          </div>

          <div className="mx-auto mt-8 max-w-3xl">
            <CodeRef
              file="src/dbsearch/controlplane/agent.py"
              code={SNIPPET_AIRGAP}
            />
          </div>
        </div>
      </section>

      {/* Proven by tests */}
      <section id="law-1-tests" className="mx-auto max-w-6xl scroll-mt-20 px-6 py-24">
        <SectionHeading
          kicker="Law 1 · verification"
          title="Tests that try to smuggle content out"
          sub="The boundary isn't a diagram, it's an executable contract. The self-test suite attacks it with payloads that hide content in every way we could think of, and each one must be rejected."
        />

        <div className="mx-auto mt-16 grid max-w-4xl gap-6 sm:grid-cols-2">
          <div className="rounded-lg border border-destructive/40 bg-surface p-6">
            <p className="font-mono text-xs text-fg">
              tests/selftest_boundary.py
            </p>
            <ul className="mt-5 space-y-3">
              {SMUGGLE_TESTS.map((item) => (
                <li
                  key={item}
                  className="flex items-start gap-3 text-sm text-fg"
                >
                  <X
                    className="mt-0.5 h-4 w-4 shrink-0 text-destructive"
                    aria-hidden="true"
                  />
                  {item}
                </li>
              ))}
            </ul>
            <p className="mt-5 text-sm text-fg-muted">
              Every one of these must raise{" "}
              <span className="font-mono text-xs">BoundaryViolation</span>, or
              the suite fails.
            </p>
          </div>

          <div className="rounded-lg border border-accent/40 bg-surface p-6">
            <p className="font-mono text-xs text-fg">
              tests/selftest_controlplane.py
            </p>
            <ul className="mt-5 space-y-3">
              <li className="flex items-start gap-3 text-sm text-fg">
                <Check
                  className="mt-0.5 h-4 w-4 shrink-0 text-accent"
                  aria-hidden="true"
                />
                Legitimate telemetry crosses and is metered
              </li>
              <li className="flex items-start gap-3 text-sm text-fg">
                <Check
                  className="mt-0.5 h-4 w-4 shrink-0 text-accent"
                  aria-hidden="true"
                />
                An air-gapped tenant answers questions locally while sending
                zero telemetry
              </li>
              <li className="flex items-start gap-3 text-sm text-fg">
                <Check
                  className="mt-0.5 h-4 w-4 shrink-0 text-accent"
                  aria-hidden="true"
                />
                A violating payload is recorded as a reason only, never
                stored
              </li>
            </ul>
            <p className="mt-5 text-sm text-fg-muted">
              For the plain-language version of these guarantees, see{" "}
              <Link
                href="/security"
                className="text-fg underline underline-offset-4 hover:text-accent"
              >
                Security
              </Link>
              .
            </p>
          </div>
        </div>
      </section>

      {/* Law two: the setup */}
      <section id="law-2" className="scroll-mt-20 border-t border-border bg-surface">
        <div className="mx-auto max-w-6xl px-6 py-24">
          <SectionHeading
            kicker="Law 2 · permission-faithful retrieval"
            title="Only answers you were already allowed to see"
            sub="Alice sits in the Directors group. Bob sits in Sales. A confidential Product A sales report is readable by Directors only. When Bob asks what the sales for Product A are, the system must not retrieve that report - even though it is the most relevant document in the index."
          />

          <div className="mx-auto mt-16 grid max-w-4xl gap-6 lg:grid-cols-2">
            <div className="rounded-lg border border-border bg-bg p-6">
              <p className="text-micro font-mono text-fg-muted">
                Timeline A - ingestion, once
              </p>
              <p className="mt-3 text-sm text-fg-muted">
                Ingestion runs as a service account that is authorised to read
                the company&apos;s documents. For every document it extracts
                two things:
              </p>
              <ol className="mt-2 list-decimal space-y-1 pl-5 text-sm text-fg-muted">
                <li>the document content, and</li>
                <li>
                  the document&apos;s access control list from the source
                  system.
                </li>
              </ol>
              <p className="mt-3 text-sm text-fg-muted">
                Suppose SharePoint says the Product A sales report is
                accessible only by the Directors group. The pipeline copies
                that ACL onto every chunk created from the document, so every
                chunk now carries:
              </p>
              <p className="mt-3 rounded-md bg-surface-muted px-3 py-2 font-mono text-xs text-fg">
                allowed_principals = [&quot;grp-directors&quot;]
              </p>
              <p className="mt-3 text-sm text-fg-muted">
                Every chunk simply carries the label of who is allowed to read
                it. And a chunk with an empty ACL matches nobody: unknown
                means hidden, not visible.
              </p>
            </div>
            <div className="rounded-lg border border-border bg-bg p-6">
              <p className="text-micro font-mono text-fg-muted">
                Timeline B - query time, every question
              </p>
              <p className="mt-3 text-sm text-fg-muted">
                Now Bob asks: &quot;What are the sales for Product A?&quot;
                His identity comes from his authenticated bearer token - never
                the request body - and his group memberships are expanded:
              </p>
              <p className="mt-3 rounded-md bg-surface-muted px-3 py-2 font-mono text-xs text-fg">
                principals = [&quot;bob&quot;, &quot;grp-sales&quot;]
              </p>
              <p className="mt-3 text-sm text-fg-muted">
                Those principals go into the search engine with the query
                embedding, and the engine compares them against the ACL on
                every chunk before anything is returned.
              </p>
            </div>
          </div>

          <div className="mx-auto mt-6 max-w-4xl">
            <CodeRef
              file="the worked example"
              code={SNIPPET_INTERSECT}
              note="Because the chunk is never returned, the object store never fetches its text, the reranker never processes it, and the model never sees it. The answer is simply that nothing accessible was found."
            />
          </div>
        </div>
      </section>

      {/* Law two: the four layers */}
      <section id="law-2-layers" className="mx-auto max-w-6xl scroll-mt-20 px-6 py-24">
        <SectionHeading
          kicker="Law 2 · defense in depth"
          title="Four layers, each independently fatal to a leak"
          sub="Permission enforcement isn't one place you could forget. It's four, and any single one of them stops Bob on its own."
        />

        <div className="mx-auto mt-16 grid max-w-5xl gap-6 lg:grid-cols-2">
          <div className="flex min-w-0 flex-col gap-4">
            <div>
              <p className="text-micro font-mono text-fg-muted">Layer 1</p>
              <h3 className="mt-2 text-lg font-semibold text-fg">
                ACL stamping at ingest
              </h3>
              <p className="mt-2 text-sm text-fg-muted">
                During ingestion, every chunk is tagged with the
                document&apos;s allowed principals. So permissions travel
                together with the data itself.
              </p>
            </div>
            <CodeRef file="src/dbsearch/pipeline/runner.py" code={SNIPPET_STAMP} />
          </div>

          <div className="flex min-w-0 flex-col gap-4">
            <div>
              <p className="text-micro font-mono text-fg-muted">Layer 2</p>
              <h3 className="mt-2 text-lg font-semibold text-fg">
                Server-side search filtering
              </h3>
              <p className="mt-2 text-sm text-fg-muted">
                When searching, the caller&apos;s principals are mandatory.
                The search index only returns chunks whose ACL intersects
                with the caller&apos;s principals. Unauthorised chunks are
                rejected directly by the search engine before they ever leave
                the database.
              </p>
            </div>
            <CodeRef file="src/dbsearch/ports/base.py" code={SNIPPET_SEARCH_SIG} />
          </div>

          <div className="flex min-w-0 flex-col gap-4 lg:col-span-2">
            <div>
              <p className="text-micro font-mono text-fg-muted">Layer 3</p>
              <h3 className="mt-2 text-lg font-semibold text-fg">
                Model isolation
              </h3>
              <p className="mt-2 text-sm text-fg-muted">
                Think of a bank vault.
              </p>
            </div>
            <div className="grid gap-4 lg:grid-cols-2">
              <div className="min-w-0 rounded-lg border border-destructive/40 bg-surface p-5">
                <p className="flex items-center gap-2 font-mono text-xs font-semibold uppercase tracking-widest text-destructive">
                  <X className="h-4 w-4" aria-hidden="true" />
                  The wrong design
                </p>
                <VaultChain steps={VAULT_WRONG} />
                <p className="mt-4 text-sm text-fg-muted">
                  You still opened everyone else&apos;s boxes. The store
                  fetched everything, confidential documents included, so that
                  text has already passed through your application&apos;s
                  memory, logs and cache. Filtering before display is too
                  late: the exposure already happened.
                </p>
              </div>
              <div className="min-w-0 rounded-lg border border-accent/40 bg-surface p-5">
                <p className="flex items-center gap-2 font-mono text-xs font-semibold uppercase tracking-widest text-accent">
                  <Check className="h-4 w-4" aria-hidden="true" />
                  What we do
                </p>
                <VaultChain steps={VAULT_RIGHT} />
                <p className="mt-4 text-sm text-fg-muted">
                  Text is fetched only for chunks that survived the trim, and
                  reranking is strictly subtractive: it can remove chunks,
                  never add authorization. The guarantee: the model only ever
                  receives already-authorized text. Even if it could answer
                  from a confidential document, it physically can&apos;t -
                  that document never reaches it.
                </p>
              </div>
            </div>
            <CodeRef file="src/dbsearch/query/service.py" code={SNIPPET_RETRIEVE} />
          </div>

          <div className="flex min-w-0 flex-col gap-4 lg:col-span-2">
            <div>
              <p className="text-micro font-mono text-fg-muted">Layer 4</p>
              <h3 className="mt-2 text-lg font-semibold text-fg">
                Trusted identity only
              </h3>
              <p className="mt-2 text-sm text-fg-muted">
                Finally, the user&apos;s identity comes from a verified bearer
                token rather than the request body. That means Bob cannot
                simply send:
              </p>
              <p className="mt-3 max-w-max rounded-md bg-surface-muted px-3 py-2 font-mono text-xs text-fg">
                &quot;user&quot;: &quot;alice&quot;
              </p>
              <p className="mt-3 text-sm text-fg-muted">
                and impersonate Alice. Only identities verified by the
                authentication system are used throughout the pipeline.
              </p>
            </div>
            <CodeRef file="tests/selftest_gqlauth.py" code={SNIPPET_IMPERSONATE} />
          </div>
        </div>
      </section>

      {/* Law two: proven by tests */}
      <section id="law-2-tests" className="scroll-mt-20 border-t border-border bg-surface">
        <div className="mx-auto max-w-6xl px-6 py-24">
          <SectionHeading
            kicker="Law 2 · verification"
            title="Alice and Bob ask the same question"
            sub="The spine self-test seeds a confidential Falcon document restricted to a deal team. Alice is on the team, Bob is not, and both ask about it. The suite fails loudly if Bob ever sees a trace of it."
          />

          <div className="mx-auto mt-16 grid max-w-4xl gap-6 lg:grid-cols-[2fr_3fr]">
            <div className="rounded-lg border border-border bg-bg p-6">
              <p className="font-mono text-xs text-fg">
                tests/selftest_spine.py
              </p>
              <ul className="mt-5 space-y-3">
                <li className="flex items-start gap-3 text-sm text-fg">
                  <Check
                    className="mt-0.5 h-4 w-4 shrink-0 text-accent"
                    aria-hidden="true"
                  />
                  Alice gets a cited answer that includes the Falcon doc
                </li>
                <li className="flex items-start gap-3 text-sm text-fg">
                  <X
                    className="mt-0.5 h-4 w-4 shrink-0 text-destructive"
                    aria-hidden="true"
                  />
                  Bob&apos;s retrieval and citations must not contain it
                </li>
                <li className="flex items-start gap-3 text-sm text-fg">
                  <Check
                    className="mt-0.5 h-4 w-4 shrink-0 text-accent"
                    aria-hidden="true"
                  />
                  A third assertion hits the index directly with Bob&apos;s
                  groups, proving the trim is server-side rather than bolted
                  on after
                </li>
              </ul>
            </div>
            <CodeRef file="tests/selftest_spine.py" code={SNIPPET_FALCON} />
          </div>
        </div>
      </section>

      {/* The remaining laws, briefly */}
      <section className="mx-auto max-w-6xl px-6 py-24">
        <SectionHeading
          kicker="Laws 3 to 8"
          title="Six more laws, at a glance"
          sub="The two above are walked in depth because they carry the trust story. The codebase holds six more, enforced the same way: a change that would violate one is redesigned, never shipped. When the repository is public, each of these will link to its own documentation."
        />

        <div className="mt-16 grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
          {OTHER_LAWS.map(({ id, num, title, body }) => (
            <div
              key={num}
              id={id}
              className="scroll-mt-24 rounded-lg border border-border bg-surface p-6"
            >
              <p className="text-micro font-mono text-fg-muted">{num}</p>
              <h3 className="mt-2 text-base font-semibold text-fg">{title}</h3>
              <p className="mt-2 text-sm text-fg-muted">{body}</p>
            </div>
          ))}
        </div>
      </section>

      <CtaBand />
    </main>
  );
}
