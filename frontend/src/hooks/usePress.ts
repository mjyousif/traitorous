import {
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';
import { apiFetch, readErrorDetail } from '../lib/api';
import { type PressState, pressStore } from '../lib/pressStore';
import type {
  Power,
  PressFeed,
  PressMessage,
  PressPostRequest,
  PressReadRequest,
  PressReadResponse,
} from '../types/diplomacy';
import { totalUnread } from '../utils/pressHelpers';

/** How often the press is re-read while the game socket is down. */
export const PRESS_POLL_MS = 15000;

/** Where a feed is read from: a seat, or a spectator (`power` null) with a match's passcode. */
interface PressReader {
  gameId: string;
  power: Power | null;
  passcode?: string | null;
}

async function readPress(
  { gameId, power, passcode }: PressReader,
  after = 0,
): Promise<PressFeed> {
  // A spectator names no power: a seat named without holding it is refused.
  const res = await apiFetch('/press', {
    query: {
      game_id: gameId,
      power: power || undefined,
      after: after > 0 ? after : undefined,
    },
    passcode: !power ? passcode : undefined,
  });
  if (!res.ok) throw new Error(`Press read failed: HTTP ${res.status}`);
  return res.json();
}

/** Fetches what arrived after the newest message held, e.g. while the socket was down. */
async function catchUp(reader: PressReader): Promise<void> {
  const { gameId, power } = reader;
  const held = pressStore.getState();
  if (held.gameId !== gameId || held.power !== power) return;
  // The first read is still on its way and will bring everything.
  if (held.status === 'loading') return;
  if (held.status !== 'ready') {
    await loadAll(reader);
    return;
  }
  const last = held.messages.length
    ? held.messages[held.messages.length - 1].seq
    : 0;
  try {
    const feed = await readPress(reader, last);
    pressStore.syncReadMarks(gameId, power, feed.last_read);
    pressStore.addMessages(gameId, power, feed.messages);
  } catch {
    // The next poll or socket message tries again.
  }
}

async function loadAll(reader: PressReader): Promise<void> {
  try {
    pressStore.setFeed(reader.gameId, await readPress(reader));
  } catch {
    pressStore.setError(reader.gameId, reader.power);
  }
}

/**
 * Sends a message and adds it to the press shown. Throws an Error holding the server's reason
 * when it is refused.
 */
export async function sendPress(
  gameId: string,
  request: PressPostRequest,
): Promise<PressMessage> {
  const res = await apiFetch('/press', {
    method: 'POST',
    query: { game_id: gameId },
    json: request,
  });
  if (!res.ok) {
    throw new Error(
      await readErrorDetail(res, 'The message could not be sent.'),
    );
  }
  const message: PressMessage = await res.json();
  pressStore.addMessages(gameId, request.power, [message]);
  return message;
}

export async function markPressRead(
  gameId: string,
  request: PressReadRequest,
): Promise<PressReadResponse> {
  const res = await apiFetch('/press/read', {
    method: 'POST',
    query: { game_id: gameId },
    json: request,
  });
  if (!res.ok) throw new Error(`Press read sync failed: HTTP ${res.status}`);
  return res.json();
}

const readSocketOpen = () => pressStore.getState().socketOpen;

export interface PressSyncOptions {
  /** The match, or null when the screen has no press (a sandbox). */
  gameId: string | null;
  /** The seat, or null for a spectator, who reads the public box alone. */
  power: Power | null;
  /** A private match's passcode, which a spectator needs to read its press. */
  passcode?: string | null;
}

/**
 * Keeps the press store filled for the match and seat on screen: reads it when they change,
 * catches up when the socket comes back, and polls while it is down.
 * New messages otherwise arrive on the game socket (see `useDiplomacyWebSocket`).
 */
export function usePressSync({
  gameId,
  power,
  passcode = null,
}: PressSyncOptions) {
  const socketOpen = useSyncExternalStore(
    pressStore.subscribe,
    readSocketOpen,
    readSocketOpen,
  );
  const press = usePressState();
  const attemptedRef = useRef<Map<string, number>>(new Map());
  const hadFailureRef = useRef(false);
  const [retryTick, setRetryTick] = useState(0);

  useEffect(() => {
    attemptedRef.current.clear();
    hadFailureRef.current = false;
  }, [gameId, power]);

  useEffect(() => {
    if (!gameId) {
      pressStore.close();
      return;
    }
    pressStore.open(gameId, power);
    loadAll({ gameId, power, passcode });
  }, [gameId, power, passcode]);

  useEffect(() => () => pressStore.close(), []);

  useEffect(() => {
    if (!gameId) return;
    if (socketOpen) {
      catchUp({ gameId, power, passcode });
      if (hadFailureRef.current) {
        hadFailureRef.current = false;
        setRetryTick((t) => t + 1);
      }
      return;
    }
    const timer = window.setInterval(() => {
      if (
        typeof document === 'undefined' ||
        document.visibilityState !== 'hidden'
      ) {
        catchUp({ gameId, power, passcode });
      }
    }, PRESS_POLL_MS);
    return () => clearInterval(timer);
  }, [gameId, power, socketOpen, passcode]);

  useEffect(() => {
    if (
      !gameId ||
      !power ||
      press.gameId !== gameId ||
      press.power !== power ||
      press.status !== 'ready'
    ) {
      return;
    }
    for (const [channel, seq] of Object.entries(press.lastRead)) {
      const known = Math.max(
        press.serverLastRead[channel] ?? 0,
        attemptedRef.current.get(channel) ?? 0,
      );
      if (seq > known) {
        attemptedRef.current.set(channel, seq);
        markPressRead(gameId, { power, channel, seq })
          .then((res) => {
            pressStore.syncReadMarks(gameId, power, res.last_read);
          })
          .catch(() => {
            if (attemptedRef.current.get(channel) === seq) {
              attemptedRef.current.delete(channel);
            }
            hadFailureRef.current = true;
          });
      }
    }
  }, [
    gameId,
    power,
    press.gameId,
    press.power,
    press.status,
    press.lastRead,
    press.serverLastRead,
    retryTick,
  ]);
}

export function usePressState(): PressState {
  return useSyncExternalStore(
    pressStore.subscribe,
    pressStore.getState,
    pressStore.getState,
  );
}

/** Whether this screen has press, and how many unread messages wait in it. */
export function usePressBadge(): { enabled: boolean; unread: number } {
  const { gameId, power, messages, lastRead } = usePressState();
  return useMemo(
    () => ({
      enabled: gameId !== null,
      unread: gameId ? totalUnread(messages, power, lastRead) : 0,
    }),
    [gameId, power, messages, lastRead],
  );
}
