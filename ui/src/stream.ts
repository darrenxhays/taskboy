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
export function useLiveData<T>(load: () => Promise<T>, deps: unknown[] = []): { data: T | null; error: string | null; reload: () => Promise<void> } {
  const { version } = useStream();
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const lastFetch = useRef(0);
  // bumped on every fetch (stream-driven or reload()); a response only applies if it's still the latest
  const requestId = useRef(0);

  const fetchNow = (): Promise<void> => {
    lastFetch.current = Date.now();
    const id = ++requestId.current;
    return load()
      .then((result) => {
        if (id === requestId.current) {
          setData(result);
          setError(null);
        }
      })
      .catch((e: Error) => {
        if (id === requestId.current) setError(e.message);
      });
  };

  useEffect(() => {
    const now = Date.now();
    // throttle stream-driven refetches to one per 1.5s
    const elapsed = now - lastFetch.current;
    if (elapsed >= 1500) {
      fetchNow();
    } else {
      const timer = setTimeout(fetchNow, 1500 - elapsed);
      return () => clearTimeout(timer);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version, ...deps]);

  return { data, error, reload: fetchNow };
}
