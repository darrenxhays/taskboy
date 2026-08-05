// tiny safe markdown subset renderer: headings, bullets, paragraphs, code fences, and safe inline formatting.
// builds react elements — no raw html ever touches the dom.

import type { ReactNode } from "react";

function inline(text: string): ReactNode[] {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\(https?:\/\/[^)\s]+\))/g);
  return parts.filter(Boolean).map((part, index) => {
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={index}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("`") && part.endsWith("`")) return <code key={index} className="mono rounded px-1 py-0.5 text-[0.92em]" style={{ background: "var(--surface-1)" }}>{part.slice(1, -1)}</code>;
    const link = part.match(/^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/);
    if (link) return <a key={index} href={link[2]} target="_blank" rel="noreferrer" className="underline underline-offset-2" style={{ color: "var(--accent)" }}>{link[1]}</a>;
    return part;
  });
}

export function Markdown({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  let list: string[] = [];
  let code: string[] | null = null;
  let key = 0;

  const flushList = () => {
    if (list.length) {
      blocks.push(
        <ul key={key++} className="my-2 list-disc space-y-1 break-words pl-5 text-[13px]" style={{ color: "var(--text-secondary)" }}>
          {list.map((item, i) => (
            <li key={i}>{inline(item)}</li>
          ))}
        </ul>,
      );
      list = [];
    }
  };

  for (const line of text.split("\n")) {
    if (code !== null) {
      if (line.startsWith("```")) {
        blocks.push(
          <pre key={key++} className="mono my-2 overflow-x-auto rounded-md border p-3 text-[12px]" style={{ borderColor: "var(--hairline)", background: "var(--surface-2)", color: "var(--text-secondary)" }}>
            {code.join("\n")}
          </pre>,
        );
        code = null;
      } else {
        code.push(line);
      }
      continue;
    }
    if (line.startsWith("```")) {
      flushList();
      code = [];
    } else if (line.startsWith("### ")) {
      flushList();
      blocks.push(
        <h4 key={key++} className="mt-3 mb-1 text-[13px] font-semibold" style={{ color: "var(--text-primary)" }}>
          {inline(line.slice(4))}
        </h4>,
      );
    } else if (line.startsWith("## ")) {
      flushList();
      blocks.push(
        <h3 key={key++} className="mt-4 mb-1 text-[14px] font-semibold" style={{ color: "var(--text-primary)" }}>
          {inline(line.slice(3))}
        </h3>,
      );
    } else if (line.startsWith("# ")) {
      flushList();
      blocks.push(
        <h2 key={key++} className="mono mt-1 mb-2 text-[15px] font-semibold" style={{ color: "var(--text-primary)" }}>
          {inline(line.slice(2))}
        </h2>,
      );
    } else if (line.startsWith("- ")) {
      list.push(line.slice(2));
    } else if (line.trim()) {
      flushList();
      blocks.push(
        <p key={key++} className="my-1.5 break-words text-[13px] leading-relaxed" style={{ color: "var(--text-secondary)" }}>
          {inline(line)}
        </p>,
      );
    } else {
      flushList();
    }
  }
  flushList();
  return <div>{blocks}</div>;
}
