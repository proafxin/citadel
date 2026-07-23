import { Eyebrow } from "@/components/eyebrow";
import { Markdown } from "@/components/markdown";
import { Card } from "@/components/ui/card";
import { getLibrary, queryStream } from "@/lib/api";
import { cn } from "@/lib/cn";
import { tierMeta } from "@/lib/tiers";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { ArrowLeft, SendHorizontal } from "lucide-react";
import { useEffect, useRef, useState } from "react";

type Message = { role: "user" | "assistant"; content: string };

export function LibraryAskPage() {
  const { libraryId } = useParams({ from: "/library/$libraryId/ask" });
  const id = Number(libraryId);
  const libQ = useQuery({ queryKey: ["library", id], queryFn: () => getLibrary(id) });
  const searchable = libQ.data ? tierMeta(libQ.data.tier).searchable : true;

  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  function patchLast(content: string) {
    setMessages((m) => {
      const copy = m.slice();
      copy[copy.length - 1] = { role: "assistant", content };
      return copy;
    });
  }

  async function send() {
    const question = input.trim();
    if (!question || busy) return;
    setInput("");
    setMessages((m) => [...m, { role: "user", content: question }, { role: "assistant", content: "" }]);
    setBusy(true);
    try {
      const res = await queryStream(id, question);
      if (!res.ok || !res.body) throw new Error(await res.text());
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let acc = "";
      let done = false;
      while (!done) {
        const chunk = await reader.read();
        done = chunk.done;
        if (chunk.value) {
          acc += decoder.decode(chunk.value, { stream: true });
          patchLast(acc);
        }
      }
      if (!acc) patchLast("_No answer._");
    } catch (err) {
      patchLast(`⚠️ ${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto flex min-h-[calc(100vh-8.5rem)] max-w-3xl flex-col">
      <Link
        to="/library/$libraryId"
        params={{ libraryId }}
        className="inline-flex items-center gap-1.5 text-sm text-ink-muted transition-colors hover:text-ink"
      >
        <ArrowLeft size={15} /> {libQ.data?.name ?? "Back to library"}
      </Link>

      <div className="mt-4">
        <Eyebrow>Ask</Eyebrow>
        <h1 className="mt-2 font-display text-3xl tracking-tight text-ink">Chat with this library</h1>
      </div>

      {!searchable ? (
        <Card className="mt-8 p-6 text-sm text-ink-muted">Search is a Tier 2 feature. Upgrade to enable chat.</Card>
      ) : (
        <>
          <div className="mt-6 flex-1 space-y-5">
            {messages.length === 0 ? <p className="text-sm text-ink-muted">Ask anything about this library.</p> : null}
            {messages.map((m, i) => (
              <div key={`m-${i}`} className={cn("flex", m.role === "user" ? "justify-end" : "justify-start")}>
                {m.role === "user" ? (
                  <div className="max-w-[85%] rounded-2xl rounded-br-sm bg-accent px-4 py-2 text-sm text-accent-ink selection:bg-accent-ink selection:text-accent">
                    {m.content}
                  </div>
                ) : (
                  <div className="max-w-full">{m.content ? <Markdown>{m.content}</Markdown> : <ThinkingDots />}</div>
                )}
              </div>
            ))}
            <div ref={bottomRef} />
          </div>

          <form
            onSubmit={(e) => {
              e.preventDefault();
              void send();
            }}
            className="sticky bottom-4 mt-6"
          >
            <div className="flex items-end gap-2 rounded-2xl border border-border bg-surface p-2 shadow-sm">
              <textarea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    void send();
                  }
                }}
                rows={1}
                placeholder="Ask a question…"
                className="max-h-40 flex-1 resize-none bg-transparent px-2 py-1.5 text-sm text-ink outline-none placeholder:text-ink-muted/60"
              />
              <button
                type="submit"
                disabled={!input.trim() || busy}
                aria-label="Send"
                className="grid size-9 shrink-0 place-items-center rounded-xl bg-accent text-accent-ink transition-colors hover:bg-accent-hover disabled:opacity-40"
              >
                <SendHorizontal size={16} />
              </button>
            </div>
          </form>
        </>
      )}
    </div>
  );
}

function ThinkingDots() {
  return (
    <div className="flex items-center gap-1 py-2">
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="size-1.5 animate-pulse rounded-full bg-ink-muted"
          style={{ animationDelay: `${i * 0.15}s` }}
        />
      ))}
    </div>
  );
}
