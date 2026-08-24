import { SOURCE_BASE } from "@/lib/source";

// A CodeRef caption is usually a repo path ("src/dbsearch/query/service.py"), sometimes with
// a line ("agent.py:40"), sometimes a COMPOUND caption whose later segments are bare
// filenames sharing the first segment's directory ("…/agent.py:40 · plane.py:41"), and
// occasionally not a path at all ("the worked example"). Linkify only the segments that are
// real source files, resolving a bare filename against the running directory; leave the rest
// as plain text so a label is never turned into a dead link.
const CODE_EXT = /\.(py|json|jsx?|tsx?|css|md|ya?ml|html|mjs)$/;
const SEG = /^(\S+?)(?::(\d+))?$/;

export function FileLink({ file }: { file: string }) {
  let dir = "";
  return (
    <>
      {file.split(" · ").map((seg, i) => {
        const sep = i > 0 ? " · " : "";
        const m = seg.match(SEG);
        const path = m?.[1] ?? "";
        const line = m?.[2];
        if (m && CODE_EXT.test(path)) {
          const full = path.includes("/") ? path : dir ? `${dir}/${path}` : path;
          if (path.includes("/")) dir = path.slice(0, path.lastIndexOf("/"));
          const href = `${SOURCE_BASE}${full}${line ? `#L${line}` : ""}`;
          return (
            <span key={i}>
              {sep}
              <a
                href={href}
                target="_blank"
                rel="noreferrer"
                className="underline decoration-border underline-offset-2 transition-colors hover:text-fg hover:decoration-fg"
              >
                {seg}
              </a>
            </span>
          );
        }
        return (
          <span key={i}>
            {sep}
            {seg}
          </span>
        );
      })}
    </>
  );
}
