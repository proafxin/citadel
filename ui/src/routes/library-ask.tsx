import { Eyebrow } from "@/components/eyebrow";
import { Markdown } from "@/components/markdown";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { getLibrary, queryStream } from "@/lib/api";
import { cn } from "@/lib/cn";
import { tierMeta } from "@/lib/tiers";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { ArrowLeft, Download, SendHorizontal, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

type Message = { role: "user" | "assistant"; content: string };

function chatKey(libraryId: number): string {
  return `citadel-chat-${libraryId}`;
}

function isMessage(value: unknown): value is Message {
  const m = value as Message;
  return (
    typeof value === "object" &&
    value !== null &&
    (m.role === "user" || m.role === "assistant") &&
    typeof m.content === "string"
  );
}

function storedMessages(libraryId: number): Message[] {
  const raw = localStorage.getItem(chatKey(libraryId));
  if (!raw) return [];
  const parsed: unknown = JSON.parse(raw);
  return Array.isArray(parsed) ? parsed.filter(isMessage) : [];
}

function toMarkdown(messages: Message[], libraryName: string): string {
  const head = `# ${libraryName}\n\n_Exported ${new Date().toLocaleString()}_\n`;
  const body = messages
    .map((m) => `## ${m.role === "user" ? "Question" : "Answer"}\n\n${m.content.trim()}`)
    .join("\n\n---\n\n");
  return `${head}\n${body}\n`;
}

function slugify(value: string): string {
  return (
    value
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || "library"
  );
}

function downloadMarkdown(text: string, filename: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export function LibraryAskPage() {
  const { libraryId } = useParams({ from: "/library/$libraryId/ask" });
  const id = Number(libraryId);
  const libQ = useQuery({ queryKey: ["library", id], queryFn: () => getLibrary(id) });
  const searchable = libQ.data ? tierMeta(libQ.data.tier).searchable : true;

  const [messages, setMessages] = useState<Message[]>(() => storedMessages(id));
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setMessages(storedMessages(id));
  }, [id]);

  useEffect(() => {
    if (busy) return;
    if (messages.length === 0) localStorage.removeItem(chatKey(id));
    else localStorage.setItem(chatKey(id), JSON.stringify(messages));
  }, [busy, id, messages]);

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

      <div className="mt-4 flex items-end justify-between gap-4">
        <div>
          <Eyebrow>Ask</Eyebrow>
          <h1 className="mt-2 font-display text-3xl tracking-tight text-ink">Chat with this library</h1>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={messages.length === 0 || busy}
            onClick={() => {
              const name = libQ.data?.name ?? "Library";
              downloadMarkdown(toMarkdown(messages, name), `${slugify(name)}-chat.md`);
            }}
          >
            <Download size={15} /> Export
          </Button>
          <Button variant="ghost" size="sm" disabled={messages.length === 0 || busy} onClick={() => setMessages([])}>
            <Trash2 size={15} /> Clear
          </Button>
        </div>
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
