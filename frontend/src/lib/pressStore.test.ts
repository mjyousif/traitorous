// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from 'vitest';
import type { Power, PressMessage } from '../types/diplomacy';
import {
  loadReadMarks,
  PUBLIC_CHANNEL,
  saveReadMarks,
} from '../utils/pressHelpers';
import { pressStore } from './pressStore';

const msg = (seq: number, sender: Power, body = `m${seq}`): PressMessage => ({
  id: `${body}-${seq}`,
  seq,
  sender,
  anonymous: false,
  recipients: [],
  body,
  sent_at: '2026-11-15T12:00:00+00:00',
  season: 'Spring',
  year: 1901,
  phase: 'Movement',
  orders: [],
});

beforeEach(() => {
  localStorage.clear();
  pressStore.reset();
});

describe('press read marks', () => {
  it('keep per-player read marks unchanged across feeds', () => {
    saveReadMarks('g1', 'FRANCE', { [PUBLIC_CHANNEL]: 1 });
    pressStore.open('g1', 'FRANCE');
    pressStore.setFeed('g1', {
      power: 'FRANCE',
      messages: [msg(1, 'GERMANY'), msg(2, 'ENGLAND')],
    });
    expect(pressStore.getState().lastRead).toEqual({ [PUBLIC_CHANNEL]: 1 });
    expect(loadReadMarks('g1', 'FRANCE')).toEqual({ [PUBLIC_CHANNEL]: 1 });

    pressStore.markRead(PUBLIC_CHANNEL, 2);
    expect(pressStore.getState().lastRead).toEqual({ [PUBLIC_CHANNEL]: 2 });
    expect(loadReadMarks('g1', 'FRANCE')).toEqual({ [PUBLIC_CHANNEL]: 2 });
  });

  it('merges server last_read monotonically on setFeed and syncReadMarks', () => {
    saveReadMarks('g1', 'FRANCE', { [PUBLIC_CHANNEL]: 3 });
    pressStore.open('g1', 'FRANCE');
    pressStore.setFeed('g1', {
      power: 'FRANCE',
      messages: [msg(1, 'GERMANY'), msg(2, 'ENGLAND')],
      last_read: { [PUBLIC_CHANNEL]: 2, 'FRANCE+GERMANY': 5 },
    });
    expect(pressStore.getState().lastRead).toEqual({
      [PUBLIC_CHANNEL]: 3,
      'FRANCE+GERMANY': 5,
    });
    expect(pressStore.getState().serverLastRead).toEqual({
      [PUBLIC_CHANNEL]: 2,
      'FRANCE+GERMANY': 5,
    });
    expect(loadReadMarks('g1', 'FRANCE')).toEqual({
      [PUBLIC_CHANNEL]: 3,
      'FRANCE+GERMANY': 5,
    });

    // Ignores updates for another game or seat.
    pressStore.syncReadMarks('g2', 'FRANCE', { [PUBLIC_CHANNEL]: 99 });
    pressStore.syncReadMarks('g1', 'GERMANY', { [PUBLIC_CHANNEL]: 99 });
    expect(pressStore.getState().lastRead[PUBLIC_CHANNEL]).toBe(3);

    // Advances cleanly and never moves backwards.
    pressStore.syncReadMarks('g1', 'FRANCE', {
      [PUBLIC_CHANNEL]: 6,
      'FRANCE+GERMANY': 1,
    });
    expect(pressStore.getState().lastRead).toEqual({
      [PUBLIC_CHANNEL]: 6,
      'FRANCE+GERMANY': 5,
    });
    expect(pressStore.getState().serverLastRead).toEqual({
      [PUBLIC_CHANNEL]: 6,
      'FRANCE+GERMANY': 5,
    });
  });
});
