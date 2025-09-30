"use client";
import { Answer } from "@/app/components/answer";
import { Relates } from "@/app/components/relates";
import { Sources } from "@/app/components/sources";
import { Relate } from "@/app/interfaces/relate";
import { Source } from "@/app/interfaces/source";
import { parseStreaming } from "@/app/utils/parse-streaming";
import { Annoyed } from "lucide-react";
import { FC, useEffect, useState } from "react";
import Locale, { getLang } from "../locales";
import { connectFanout } from "@/app/utils/fanout"; // <-- added

export const Result: FC<{ query: string; rid: string }> = ({ query, rid }) => {
  const [sources, setSources] = useState<Source[]>([]);
  const [markdown, setMarkdown] = useState<string>("");
  const [relates, setRelates] = useState<Relate[] | null>(null);
  const [error, setError] = useState<number | null>(null);

  useEffect(() => {
    if (!query) return;

    const controller = new AbortController();
    const useFanout = process.env.NEXT_PUBLIC_USE_FANOUT === "1";

    // reset UI for new query
    setSources([]);
    setMarkdown("");
    setRelates(null);
    setError(null);

    if (useFanout) {
      // Kick the backend to start the job; then subscribe via SSE at the edge
      (async () => {
        try {
          await fetch(`/query`, {
            method: "POST",
            headers: { "Content-Type": "application/json", Accept: "*/*" },
            signal: controller.signal,
            body: JSON.stringify({
              query,
              search_uuid: rid,
              lang: getLang(),
            }),
          });
        } catch (_e) {
          // even if this POST errors locally, the job might already be running
        }

        const disconnect = connectFanout(rid, {
          onSources: (v: any) => setSources(v),
          onMarkdown: (t: string) => setMarkdown((prev) => prev + t),
          onRelates: (v: any) => setRelates(v),
          onError: (s?: number) => setError(s ?? 499),
        });

        return () => {
          disconnect();
          if (!controller.signal.aborted) controller.abort();
        };
      })();

      return () => {
        if (!controller.signal.aborted) controller.abort();
      };
    }

    // ---- Non-Fanout (existing) path ----
    parseStreaming(
      controller,
      query,
      rid,
      getLang(),
      setSources,
      (t: string) => setMarkdown((prev) => prev + t),
      setRelates,
      setError,
    ).catch((err: any) => {
      if (err?.name === "AbortError") return;
      setError(500);
    });

    return () => {
      if (!controller.signal.aborted) controller.abort();
    };
  }, [query, rid]);

  return (
    <div className="flex flex-col gap-8">
      <Answer markdown={markdown} sources={sources}></Answer>
      <Sources sources={sources}></Sources>
      <Relates relates={relates}></Relates>
      {error && (
        <div className="absolute inset-4 flex items-center justify-center bg-white/40 backdrop-blur-sm">
          <div className="p-4 bg-white shadow-2xl rounded text-blue-500 font-medium flex gap-4">
            <Annoyed></Annoyed>
            {error === 429 ? Locale.Err[429] : Locale.Err[500]}
          </div>
        </div>
      )}
    </div>
  );
};
