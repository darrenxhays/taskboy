// one sse connection for the whole app: pages subscribe to live task events and count changes

import { createContext, useContext, useEffect, useRef, useState } from "react";
import type { TaskEvent } from "./api";

export type StreamState = {
  connected: boolean;
  counts: Record<string, number> | null;
  version: number; // bumps on every task event — pages refetch on change (debounced by the browser's batching)
  lastEvent: TaskEvent | null;
};

export const StreamContext = createContext<StreamState>({ connected: false, counts: null, version: 0, lastEvent: null });

export function useStream(): StreamState {
  return useContext(StreamContext);
}

export function useStreamSource(): StreamState {
  const [state, setState] = useState<StreamState>({ connected: false, counts: null, version: 0, lastEvent: null });
  const versionRef = useRef(0);

  useEffect(() => {
    const source = new EventSource("/api/stream");
    source.onopen = () => setState((previous) => ({ ...previous, connected: true }));
    source.onerror = () => setState((previous) => ({ ...previous, connected: false }));
    source.addEventListener("task_event", (message) => {
      versionRef.current += 1;
      const event = JSON.parse((message as MessageEvent).data) as TaskEvent;
      setState((previous) => ({ ...previous, version: versionRef.current, lastEvent: event }));
    });
    source.addEventListener("counts", (message) => {
      const counts = JSON.parse((message as MessageEvent).data) as Record<string, number>;
      setState((previous) => ({ ...previous, counts }));
    });
    return () => source.close();
  }, []);

  return state;
}

// refetch helper: runs load() now and again whenever the stream version changes (throttled)
export function useLiveData<T>(load: () => Promise<T>, deps: unknown[] = []): { data: T | null; error: string | null; reload: () => void } {
  const { version } = useStream();
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const lastFetch = useRef(0);
  const pending = useRef(false);

  useEffect(() => {
    let cancelled = false;
    const now = Date.now();
    const run = () => {
      lastFetch.current = Date.now();
      pending.current = false;
      load()
        .then((result) => {
          if (!cancelled) {
            setData(result);
            setError(null);
          }
        })
        .catch((e: Error) => {
          if (!cancelled) setError(e.message);
        });
    };
    // throttle stream-driven refetches to one per 1.5s
    const elapsed = now - lastFetch.current;
    if (elapsed >= 1500) {
      run();
    } else if (!pending.current) {
      pending.current = true;
      const timer = setTimeout(run, 1500 - elapsed);
      return () => {
        cancelled = true;
        clearTimeout(timer);
      };
    }
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version, nonce, ...deps]);

  return { data, error, reload: () => setNonce((n) => n + 1) };
}
