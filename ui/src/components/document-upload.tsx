import { useMutation, useQueryClient } from "@tanstack/react-query";
import { UploadCloud } from "lucide-react";
import { useRef, useState } from "react";
import { uploadDocuments } from "@/lib/api";
import { cn } from "@/lib/cn";

export function DocumentUpload({ libraryId }: { libraryId: number }) {
  const qc = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  const mutation = useMutation({
    mutationFn: (files: File[]) => uploadDocuments(libraryId, files),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["documents", libraryId] });
      qc.invalidateQueries({ queryKey: ["library", libraryId] });
    },
  });

  function send(list: FileList | null) {
    if (!list || list.length === 0) return;
    mutation.mutate(Array.from(list));
  }

  return (
    <div>
      <button
        type="button"
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          send(e.dataTransfer.files);
        }}
        className={cn(
          "flex w-full flex-col items-center justify-center gap-2 rounded-xl border border-dashed px-6 py-8 text-center transition-colors",
          dragging ? "border-accent bg-surface-2" : "border-border hover:border-ink-muted",
        )}
      >
        <UploadCloud size={22} className={cn("transition-colors", dragging ? "text-accent" : "text-ink-muted")} />
        <span className="text-sm text-ink">
          {mutation.isPending ? "Uploading…" : "Drop files here, or click to browse"}
        </span>
        <span className="text-xs text-ink-muted">PDF, Word, Excel, CSV, HTML — multiple at once</span>
      </button>
      <input
        ref={inputRef}
        type="file"
        multiple
        hidden
        onChange={(e) => {
          send(e.target.files);
          e.target.value = "";
        }}
      />
      {mutation.isError ? <p className="mt-2 text-sm text-red-400">{(mutation.error as Error).message}</p> : null}
    </div>
  );
}
