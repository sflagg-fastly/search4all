export function connectFanout(
  channel: string,
  {
    onSources,
    onMarkdown,
    onRelates,
    onError,
  }: {
    onSources: (v: any) => void;
    onMarkdown: (v: string) => void;
    onRelates: (v: any) => void;
    onError?: (s: number) => void;
  },
) {
  const es = new EventSource(`/stream/${channel}`, { withCredentials: false });

  es.addEventListener('sources', (e: MessageEvent) => {
    onSources(JSON.parse(e.data));
  });

  es.addEventListener('marker', (_e) => {
    // optional UI marker
  });

  es.addEventListener('delta', (e) => {
    onMarkdown(e.data ? JSON.parse(e.data).text : '');
  });

  es.addEventListener('related', (e) => {
    onRelates(JSON.parse(e.data));
  });

  es.addEventListener('done', (_e) => es.close());

  es.onerror = (_e) => {
    es.close();
    onError?.(499);
  };

  return () => es.close();
}
