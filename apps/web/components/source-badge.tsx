import type { Citation } from "@/lib/types";

export function SourceBadge({ citation }: { citation: Citation }) {
  const content = (
    <>
      <span className="source-check">✓</span>
      <span>{citation.source}</span>
      {(citation.as_of ?? citation.retrieved_at) && (
        <time>{new Date(citation.as_of ?? citation.retrieved_at ?? "").toLocaleDateString()}</time>
      )}
    </>
  );
  return citation.url ? (
    <a className="source-badge" href={citation.url} rel="noreferrer" target="_blank">
      {content}
    </a>
  ) : (
    <span className="source-badge">{content}</span>
  );
}
