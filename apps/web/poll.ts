import { useEffect, useState } from 'react';

// One sequential loop per data surface; no overlapping requests or stale team results.
export function usePoll<T>(identity: string, load: (signal: AbortSignal) => Promise<T>, interval = 5000) {
  const [state, setState] = useState<{ identity: string; data?: T; error?: string }>({ identity });
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>, failures = 0;
    setState({ identity });
    async function tick() {
      try {
        const data = await load(controller.signal);
        if (!controller.signal.aborted) { setState({ identity, data }); failures = 0; }
      } catch (error) {
        if (!controller.signal.aborted) {
          failures++;
          setState(previous => ({ ...previous, identity, error: error instanceof Error ? error.message : 'Temporarily unavailable.' }));
        }
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(tick,
          Math.max(document.hidden ? 30000 : interval, Math.min(60000, interval * 2 ** failures)));
      }
    }
    void tick();
    return () => { controller.abort(); clearTimeout(timer); };
    // load is intentionally scoped to the caller's explicit identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identity, interval]);
  return state.identity === identity ? state : { identity };
}
