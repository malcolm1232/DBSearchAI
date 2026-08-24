// #952 - the REAL chatStream (api.js) must SETTLE on every way a stream can end.
//
// The wedge: the server's SSE died mid-flight (a Groq 429 raised inside the generator after
// the 200 was sent) and chatStream just returned with onDone never called - typing dots
// forever - or, when the proxy held the socket half-open, never settled at all and left
// `busy` stuck so the input stayed dead. Three cases, driven through the real function with
// fetch stubbed:
//   errorEvent  - a terminal {"type":"error"} event must REJECT with its message
//   abruptEnd   - a stream that ends with neither done nor error must REJECT (never a
//                 silent no-answer resolve)
//   healthy     - token + done resolves, onDone fired (the control)
//
// Read by tests/selftest_952_stream_error_event.py; reports JSON on stdout.
import { pathToFileURL } from "node:url";

const [, , apiPath] = process.argv;

function sseResponse(lines) {
  const enc = new TextEncoder();
  const chunks = lines.map((l) => enc.encode(l));
  let i = 0;
  return {
    ok: true, status: 200,
    body: {
      getReader: () => ({
        read: async () =>
          i < chunks.length ? { value: chunks[i++], done: false } : { value: undefined, done: true },
      }),
    },
  };
}

let nextResponse = null;
globalThis.fetch = async () => nextResponse;

const { chatStream } = await import(pathToFileURL(apiPath).href);
const out = {};

// 1. a terminal error event rejects with its message
nextResponse = sseResponse([
  'data: {"type":"token","text":"Hel"}\n\n',
  'data: {"type":"error","message":"The model is rate-limited right now - wait a few seconds and ask again."}\n\n',
]);
try {
  await chatStream("cv", "q", () => {}, () => {});
  out.errorEventRejects = false;
} catch (e) {
  out.errorEventRejects = true;
  out.errorMessage = e.message;
}

// 2. THE WEDGE: the stream just ends - no done, no error. Must reject, never resolve silently.
nextResponse = sseResponse(['data: {"type":"token","text":"Hel"}\n\n']);
try {
  await chatStream("cv", "q", () => {}, () => {});
  out.abruptEndRejects = false;
} catch (e) {
  out.abruptEndRejects = true;
  out.abruptMessage = e.message;
}

// 3. the control: a healthy stream resolves and onDone fires
let doneFired = false;
nextResponse = sseResponse([
  'data: {"type":"token","text":"Hi"}\n\n',
  'data: {"type":"done","answer":"Hi","citations":[],"retrieved_docs":[]}\n\n',
]);
try {
  await chatStream("cv", "q", () => {}, () => { doneFired = true; });
  out.healthyResolves = true;
} catch (e) {
  out.healthyResolves = false;
  out.healthyError = e.message;
}
out.healthyDoneFired = doneFired;

console.log(JSON.stringify(out, null, 1));
process.exit(0);
