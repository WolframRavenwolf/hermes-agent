import test from 'node:test';
import assert from 'node:assert/strict';

import { getAudioMessage, getMessageContent } from './media-utils.js';

const audio = { mimetype: 'audio/ogg; codecs=opus' };
const ptt = { mimetype: 'audio/mpeg', ptt: true };

test('returns direct audio and PTT messages', () => {
  assert.equal(getAudioMessage({ message: { audioMessage: audio } }), audio);
  assert.equal(getAudioMessage({ message: { pttMessage: ptt } }), ptt);
});

test('unwraps ephemeral and view-once audio messages', () => {
  const ephemeral = {
    message: { ephemeralMessage: { message: { audioMessage: audio } } },
  };
  const viewOnce = {
    message: { viewOnceMessageV2: { message: { pttMessage: ptt } } },
  };

  assert.equal(getAudioMessage(ephemeral), audio);
  assert.equal(getAudioMessage(viewOnce), ptt);
});

test('unwraps nested wrappers with a bounded traversal', () => {
  const nested = {
    message: {
      ephemeralMessage: {
        message: {
          viewOnceMessage: { message: { audioMessage: audio } },
        },
      },
    },
  };

  assert.deepEqual(getMessageContent(nested), { audioMessage: audio });
  assert.equal(getAudioMessage(nested), audio);
});

test('returns null when no audio message is present', () => {
  assert.equal(getAudioMessage({ message: { conversation: 'hello' } }), null);
});
