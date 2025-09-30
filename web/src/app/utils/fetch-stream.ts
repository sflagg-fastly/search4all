const decoder = new TextDecoder();

async function pump(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  controller: ReadableStreamDefaultController<string>,
  onChunk?: (chunk: Uint8Array) => void,
  onDone?: () => void,
): Promise<void> {
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        onDone && onDone();
        // flush any pending decoder bytes
        const tail = decoder.decode();
        if (tail) controller.enqueue(tail);
        controller.close();
        break;
      }
      if (value) {
        onChunk && onChunk(value);
        // stream decode to avoid re-chunking issues
        const text = decoder.decode(value, { stream: true });
        if (text) controller.enqueue(text);
      }
    }
  } catch (err) {
    controller.error(err);
  } finally {
    try { reader.releaseLock(); } catch {}
  }
}

export const fetchStream = (
  response: Response,
  onChunk?: (chunk: Uint8Array) => void,
  onDone?: () => void,
): ReadableStream<string> => {
  const reader = response.body!.getReader();
  return new ReadableStream<string>({
    start(controller) {
      void pump(reader, controller, onChunk, onDone);
    },
    cancel() {
      try { reader.cancel(); } catch {}
      try { decoder.decode(); } catch {} // finalize decoder
    },
  });
};
