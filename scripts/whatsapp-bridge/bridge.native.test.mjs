/**
 * Unit tests for WhatsApp-native bridge payload helpers.
 *
 * These tests avoid importing bridge.js because that file starts an HTTP
 * server and Baileys socket at module load. Keep the helper module pure.
 */

import { strict as assert } from 'node:assert';
import { createHash } from 'node:crypto';
import { mkdtempSync, unlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { getAggregateVotesInPollMessage } from '@whiskeysockets/baileys';

import {
  BRIDGE_RUNTIME_FILES,
  bridgeDependencyFingerprint,
  buildPollPayload,
  bridgeSourceHash,
  buildTextSendPayload,
  createBoundedMessageStore,
  appendMediaFailureNote,
  extractBridgeEvent,
  inboundReadReceiptKeys,
  framedFileHash,
  mediaPayloadForFile,
  pollCreationMessageFromPayload,
  pollUpdateForAggregation,
  validatedRuntimeFiles,
} from './bridge_helpers.js';

// -- inbound read receipts ------------------------------------------------
{
  const groupKey = {
    id: 'incoming-group-1',
    remoteJid: '120363001234567890@g.us',
    participant: '15550001111@s.whatsapp.net',
    fromMe: false,
  };

  assert.deepEqual(inboundReadReceiptKeys({ key: groupKey, enabled: false }), []);
  assert.deepEqual(
    inboundReadReceiptKeys({ key: { ...groupKey, fromMe: true }, enabled: true }),
    [],
  );
  const receiptKeys = inboundReadReceiptKeys({ key: groupKey, enabled: true });
  assert.equal(receiptKeys.length, 1);
  assert.equal(receiptKeys[0], groupKey);
  assert.equal(receiptKeys[0].participant, groupKey.participant);
  console.log('  ✓ inbound read receipts preserve the original group message key');
}

// -- stale-bridge complete runtime source hash ----------------------------
{
  const expectedInventory = [
    'bridge.js',
    'bridge_helpers.js',
    'allowlist.js',
    'outbound_ids.js',
    'owner_message_gate.js',
    'package.json',
    'package-lock.json',
  ];
  assert.deepEqual(BRIDGE_RUNTIME_FILES, expectedInventory);

  const bridgeDir = mkdtempSync(path.join(tmpdir(), 'hermes-wa-hash-'));
  const runtimeBytes = new Map();
  for (const name of expectedInventory) {
    const bytes = name === 'package.json'
      ? Buffer.from(`${JSON.stringify({
        name: 'hermes-whatsapp-bridge-test',
        hermesRuntimeFiles: expectedInventory,
      })}\n`)
      : Buffer.from(`${name}:v1\n`);
    runtimeBytes.set(name, bytes);
    writeFileSync(path.join(bridgeDir, name), bytes);
  }
  const bridgePath = path.join(bridgeDir, 'bridge.js');
  const expected = framedFileHash(
    expectedInventory.map(name => [name, runtimeBytes.get(name)]),
    16,
  );
  const originalHash = bridgeSourceHash(bridgePath);
  assert.equal(originalHash, expected);
  assert.equal(
    bridgeDependencyFingerprint(bridgeDir),
    framedFileHash([
      ['package.json', runtimeBytes.get('package.json')],
      ['package-lock.json', runtimeBytes.get('package-lock.json')],
    ]),
  );

  for (const name of expectedInventory) {
    const originalBytes = runtimeBytes.get(name);
    writeFileSync(path.join(bridgeDir, name), Buffer.from(`${name}:v2\n`));
    assert.notEqual(
      bridgeSourceHash(bridgePath),
      originalHash,
      `${name} must participate in scriptHash`,
    );
    writeFileSync(path.join(bridgeDir, name), originalBytes);
  }

  // A partly present managed inventory is unsafe rather than a legacy bridge.
  for (const missingName of ['allowlist.js', 'package.json']) {
    const missingPath = path.join(bridgeDir, missingName);
    unlinkSync(missingPath);
    assert.equal(bridgeSourceHash(bridgePath), '', `${missingName} is required`);
    writeFileSync(missingPath, runtimeBytes.get(missingName));
  }
  writeFileSync(path.join(bridgeDir, 'package.json'), '{not valid json\n');
  assert.equal(bridgeSourceHash(bridgePath), '', 'malformed package.json is unsafe');
  writeFileSync(path.join(bridgeDir, 'package.json'), runtimeBytes.get('package.json'));

  const customDir = mkdtempSync(path.join(tmpdir(), 'hermes-wa-custom-hash-'));
  const customPath = path.join(customDir, 'custom-bridge.js');
  const customBytes = Buffer.from('const custom = true;\n');
  writeFileSync(customPath, customBytes);
  assert.equal(
    bridgeSourceHash(customPath),
    createHash('sha256').update(customBytes).digest('hex').slice(0, 16),
  );
  console.log('  ✓ bridge source hash covers the full runtime and custom single-file bridges');
}

// -- framed hash boundary ambiguity ---------------------------------------
{
  const first = [['a.js', Buffer.from('ab')], ['b.js', Buffer.from('c')]];
  const second = [['a.js', Buffer.from('a')], ['b.js', Buffer.from('bc')]];
  assert.equal(
    Buffer.concat(first.map(([, bytes]) => bytes)).toString(),
    Buffer.concat(second.map(([, bytes]) => bytes)).toString(),
    'the legacy concatenation is intentionally ambiguous',
  );
  assert.notEqual(framedFileHash(first), framedFileHash(second));
  console.log('  ✓ framed file hashes distinguish ambiguous byte boundaries');
}

// -- manifest validator parity/security -----------------------------------
{
  const requiredInventory = [
    'bridge.js',
    'bridge_helpers.js',
    'package.json',
    'package-lock.json',
  ];
  assert.deepEqual(
    validatedRuntimeFiles({ hermesRuntimeFiles: requiredInventory }),
    requiredInventory,
  );

  const invalidManifests = [
    ['leading whitespace', [...requiredInventory, ' extra.js']],
    ['trailing whitespace', [...requiredInventory, 'extra.js\t']],
    ['unicode whitespace', [...requiredInventory, '\u00a0extra.js']],
    ['control whitespace', [...requiredInventory, '\u001cextra.js']],
    ['slash', [...requiredInventory, 'nested/extra.js']],
    ['backslash', [...requiredInventory, 'nested\\extra.js']],
    ['NUL', [...requiredInventory, 'extra\0.js']],
    ['absolute path', [...requiredInventory, '/extra.js']],
    ['dot', [...requiredInventory, '.']],
    ['dot-dot', [...requiredInventory, '..']],
    ['duplicate', [...requiredInventory, 'bridge.js']],
    ['non-string', [...requiredInventory, 7]],
  ];
  for (const [label, inventory] of invalidManifests) {
    assert.equal(
      validatedRuntimeFiles({ hermesRuntimeFiles: inventory }),
      null,
      `${label} inventory must be rejected without normalization`,
    );
  }
  for (const malformed of [null, {}, { hermesRuntimeFiles: 'bridge.js' }, { hermesRuntimeFiles: [] }]) {
    assert.equal(validatedRuntimeFiles(malformed), null);
  }
  console.log('  ✓ runtime manifest rejects whitespace, traversal, NUL, duplicates, and malformed values');

}

// -- quoted outbound text -------------------------------------------------
{
  const store = createBoundedMessageStore(2);
  store.remember({
    key: {
      id: 'inbound-1',
      remoteJid: '15551234567@s.whatsapp.net',
      participant: '15550001111@s.whatsapp.net',
      fromMe: false,
    },
    message: { conversation: 'original text' },
  });

  const { content, options } = buildTextSendPayload('reply text', {
    chatId: '15551234567@s.whatsapp.net',
    replyTo: 'inbound-1',
    messageStore: store,
  });

  assert.deepEqual(content, { text: 'reply text' });
  assert.equal(options.quoted.key.id, 'inbound-1');
  assert.equal(options.quoted.message.conversation, 'original text');
  console.log('  ✓ text replies include Baileys quoted message when resolvable');
}

{
  const store = createBoundedMessageStore(2);
  const { content, options } = buildTextSendPayload('plain text', {
    chatId: '15551234567@s.whatsapp.net',
    replyTo: 'missing-id',
    messageStore: store,
  });

  assert.deepEqual(content, { text: 'plain text' });
  assert.deepEqual(options, {});
  console.log('  ✓ unresolved replyTo falls back to plain text');
}

// -- inbound quote/media/native metadata --------------------------------
{
  const event = await extractBridgeEvent({
    msg: {
      key: {
        id: 'incoming-1',
        remoteJid: '15551234567@s.whatsapp.net',
        participant: '15550001111@s.whatsapp.net',
        fromMe: false,
      },
      pushName: 'Tester',
      messageTimestamp: 123,
      message: {
        extendedTextMessage: {
          text: 'approved',
          contextInfo: {
            stanzaId: 'outbound-1',
            participant: '15559998888@s.whatsapp.net',
            remoteJid: '15551234567@s.whatsapp.net',
            quotedMessage: { conversation: 'approve deploy?' },
          },
        },
      },
    },
    chatId: '15551234567@s.whatsapp.net',
    senderId: '15550001111@s.whatsapp.net',
    senderNumber: '15550001111',
    botIds: ['15559998888@s.whatsapp.net'],
    downloadMedia: async () => Buffer.from(''),
  });

  assert.equal(event.quotedMessageId, 'outbound-1');
  assert.equal(event.quotedParticipant, '15559998888@s.whatsapp.net');
  assert.equal(event.quotedRemoteJid, '15551234567@s.whatsapp.net');
  assert.equal(event.quotedText, 'approve deploy?');
  assert.deepEqual(event.readReceiptKey, {
    id: 'incoming-1',
    remoteJid: '15551234567@s.whatsapp.net',
    participant: '15550001111@s.whatsapp.net',
    fromMe: false,
  });
  assert.equal(event.hasQuotedMessage, true);
  assert.equal(event.body, 'approved');
  console.log('  ✓ inbound quoted metadata includes quoted text');
}

{
  const event = await extractBridgeEvent({
    msg: {
      key: { id: 'doc-1', remoteJid: '15551234567@s.whatsapp.net', fromMe: false },
      messageTimestamp: 123,
      message: {
        documentMessage: {
          caption: 'see attached',
          fileName: 'report.pdf',
          mimetype: 'application/pdf',
        },
      },
    },
    chatId: '15551234567@s.whatsapp.net',
    senderId: '15550001111@s.whatsapp.net',
    senderNumber: '15550001111',
    downloadMedia: async () => Buffer.from('pdf'),
    writeMediaFile: async () => '/tmp/report.pdf',
  });

  assert.equal(event.hasMedia, true);
  assert.equal(event.mediaType, 'document');
  assert.equal(event.mime, 'application/pdf');
  assert.equal(event.fileName, 'report.pdf');
  assert.equal(event.nativeType, 'documentMessage');
  assert.deepEqual(event.mediaUrls, ['/tmp/report.pdf']);
  console.log('  ✓ inbound document metadata preserves MIME and filename');
}

{
  const cacheDir = mkdtempSync(path.join(tmpdir(), 'hermes-wa-doc-'));
  const event = await extractBridgeEvent({
    msg: {
      key: { id: 'doc-2', remoteJid: '15551234567@s.whatsapp.net', fromMe: false },
      messageTimestamp: 123,
      message: {
        documentMessage: {
          caption: 'see attached',
          fileName: 'report',
          mimetype: 'application/pdf',
        },
      },
    },
    chatId: '15551234567@s.whatsapp.net',
    senderId: '15550001111@s.whatsapp.net',
    senderNumber: '15550001111',
    downloadMedia: async () => Buffer.from('pdf'),
    cacheDirs: { document: cacheDir },
  });

  assert.equal(event.mediaUrls.length, 1);
  assert.ok(event.mediaUrls[0].endsWith('_report.pdf'), event.mediaUrls[0]);
  console.log('  ✓ MIME extension is preserved when document filename has none');
}

{
  let writerArgs = null;
  const audioDir = mkdtempSync(path.join(tmpdir(), 'hermes-wa-audio-'));
  const event = await extractBridgeEvent({
    msg: {
      key: { id: 'audio-wrapped-1', remoteJid: '15551234567@s.whatsapp.net', fromMe: false },
      messageTimestamp: 123,
      message: {
        ephemeralMessage: {
          message: {
            viewOnceMessageV2: {
              message: {
                audioMessage: {
                  mimetype: 'audio/wav',
                  ptt: false,
                },
              },
            },
          },
        },
      },
    },
    chatId: '15551234567@s.whatsapp.net',
    senderId: '15550001111@s.whatsapp.net',
    senderNumber: '15550001111',
    downloadMedia: async () => Buffer.from('wav'),
    writeMediaFile: async (args) => {
      writerArgs = args;
      return path.join(args.dir, `nested-audio${args.ext}`);
    },
    cacheDirs: { audio: audioDir },
  });

  assert.equal(event.hasMedia, true);
  assert.equal(event.mediaType, 'audio');
  assert.equal(event.mime, 'audio/wav');
  assert.equal(event.nativeType, 'audioMessage');
  assert.equal(writerArgs.ext, '.wav');
  assert.deepEqual(event.mediaUrls, [path.join(audioDir, 'nested-audio.wav')]);
  console.log('  ✓ nested ephemeral/view-once WAV audio is normalized and keeps .wav');
}

{
  const event = await extractBridgeEvent({
    msg: {
      key: { id: 'loc-1', remoteJid: '15551234567@s.whatsapp.net', fromMe: false },
      messageTimestamp: 123,
      message: {
        locationMessage: {
          name: 'HQ',
          degreesLatitude: 41.015,
          degreesLongitude: 28.979,
        },
      },
    },
    chatId: '15551234567@s.whatsapp.net',
    senderId: '15550001111@s.whatsapp.net',
    senderNumber: '15550001111',
  });

  assert.equal(event.mediaType, 'location');
  assert.equal(event.body, '[Location: HQ 41.015,28.979]');
  assert.deepEqual(event.nativeMetadata.location, {
    name: 'HQ',
    address: '',
    latitude: 41.015,
    longitude: 28.979,
    isLive: false,
  });
  console.log('  ✓ native location messages get text fallback and metadata');
}

{
  const event = await extractBridgeEvent({
    msg: {
      key: { id: 'poll-1', remoteJid: '15551234567@s.whatsapp.net', fromMe: false },
      messageTimestamp: 123,
      message: {
        pollCreationMessage: {
          name: 'Approve deploy?',
          options: [{ optionName: 'Approve' }, { optionName: 'Deny' }],
          selectableOptionsCount: 1,
        },
      },
    },
    chatId: '15551234567@s.whatsapp.net',
    senderId: '15550001111@s.whatsapp.net',
    senderNumber: '15550001111',
  });

  assert.equal(event.mediaType, 'poll');
  assert.equal(event.body, '[Poll: Approve deploy? Options: Approve, Deny]');
  assert.deepEqual(event.nativeMetadata.poll.options, ['Approve', 'Deny']);
  console.log('  ✓ poll creation messages get text fallback and metadata');
}

// -- outbound media/poll helpers -----------------------------------------
{
  const payload = mediaPayloadForFile({
    buffer: Buffer.from('gif89a'),
    filePath: '/tmp/loop.gif',
    mediaType: 'image',
    caption: 'loop',
  });

  assert.ok(payload.image, 'pure helper fallback keeps raw GIF as image bytes');
  assert.equal(payload.gifPlayback, undefined);
  assert.equal(payload.mimetype, 'image/gif');
  assert.equal(payload.caption, 'loop');
  console.log('  ✓ local GIF helper fallback stays truthful; live bridge converts to gifPlayback when possible');
}

{
  const payload = buildPollPayload({
    question: 'Proceed?',
    options: ['Approve', 'Deny'],
    selectableCount: 1,
  });

  assert.equal(payload.poll.name, 'Proceed?');
  assert.deepEqual(payload.poll.values, ['Approve', 'Deny']);
  assert.equal(payload.poll.selectableCount, 1);
  assert.equal(Buffer.isBuffer(payload.poll.messageSecret), true);
  assert.equal(payload.poll.messageSecret.length, 32);
  assert.deepEqual(pollCreationMessageFromPayload(payload), {
    messageContextInfo: {
      messageSecret: payload.poll.messageSecret,
    },
    pollCreationMessageV3: {
      name: 'Proceed?',
      options: [{ optionName: 'Approve' }, { optionName: 'Deny' }],
      selectableOptionsCount: 1,
    },
  });
  console.log('  ✓ poll payload primitive carries a cacheable vote secret');
}

{
  const pollCreation = {
    key: {
      id: 'poll-creation',
      remoteJid: '15551234567@s.whatsapp.net',
      fromMe: true,
    },
    message: {
      messageContextInfo: {
        messageSecret: Buffer.from('0123456789abcdef0123456789abcdef'),
      },
      pollCreationMessageV3: {
        name: 'Proceed?',
        options: [{ optionName: 'Approve' }, { optionName: 'Deny' }],
        selectableOptionsCount: 1,
      },
    },
  };
  const voteKey = {
    id: 'vote-message',
    remoteJid: '15551234567@s.whatsapp.net',
    participant: '15550001111@s.whatsapp.net',
    fromMe: false,
  };
  const encryptedVote = {
    encPayload: Buffer.from('payload'),
    encIv: Buffer.from('iv'),
  };

  const attempts = [];
  const pollUpdate = pollUpdateForAggregation({
    pollUpdateMessage: {
      pollCreationMessageKey: pollCreation.key,
      vote: encryptedVote,
      senderTimestampMs: 123,
    },
    pollUpdateMessageKey: voteKey,
    pollCreation,
    decryptPollVote: (vote, ctx) => {
      attempts.push({ pollCreatorJid: ctx.pollCreatorJid, voterJid: ctx.voterJid });
      assert.equal(vote, encryptedVote);
      assert.equal(ctx.pollMsgId, 'poll-creation');
      assert.equal(ctx.pollEncKey, pollCreation.message.messageContextInfo.messageSecret);
      if (ctx.pollCreatorJid !== 'creator-lid@lid') {
        throw new Error('wrong creator jid');
      }
      assert.equal(ctx.voterJid, '15550001111@s.whatsapp.net');
      return {
        selectedOptions: [createHash('sha256').update(Buffer.from('Approve')).digest()],
      };
    },
    getKeyAuthor: (key, meId = 'me') => (key?.fromMe ? meId : key?.participant || key?.remoteJid || ''),
    meId: 'classic-me@s.whatsapp.net',
    pollCreatorJids: ['classic-me@s.whatsapp.net', 'creator-lid@lid'],
  });

  assert.deepEqual(attempts.map(item => item.pollCreatorJid), ['classic-me@s.whatsapp.net', 'creator-lid@lid']);

  assert.equal(pollUpdate.pollUpdateMessageKey.id, 'vote-message');
  assert.equal(pollUpdate.senderTimestampMs, 123);
  const aggregation = getAggregateVotesInPollMessage({
    message: pollCreation.message,
    pollUpdates: [pollUpdate],
  });
  assert.deepEqual(
    aggregation.map(option => ({ name: option.name, voters: option.voters })),
    [
      { name: 'Approve', voters: ['15550001111@s.whatsapp.net'] },
      { name: 'Deny', voters: [] },
    ],
  );
  console.log('  ✓ encrypted poll upserts are wrapped into Baileys aggregation shape');
}

// -- media download failure containment (port of nanoclaw#2895) -----------
{
  assert.equal(appendMediaFailureNote('hello', []), 'hello');
  assert.equal(
    appendMediaFailureNote('check this out', ['image']),
    'check this out\n[image could not be downloaded]',
  );
  // Regression guard: an uncaptioned failed image must still produce a
  // non-empty body, or the empty-message guard drops the whole message.
  assert.equal(appendMediaFailureNote('', ['image']), '[image could not be downloaded]');
  assert.equal(
    appendMediaFailureNote('', ['image', 'document']),
    '[image could not be downloaded] [document could not be downloaded]',
  );
  console.log('  ✓ appendMediaFailureNote formats failure notes');
}

{
  // A throwing downloadMedia (expired CDN URL) must not reject out of
  // extractBridgeEvent — before this guard the whole upsert batch died and
  // the message was silently dropped.
  const event = await extractBridgeEvent({
    msg: {
      key: { id: 'img-fail-1', remoteJid: '15551234567@s.whatsapp.net', fromMe: false },
      messageTimestamp: 123,
      message: { imageMessage: { caption: '', mimetype: 'image/jpeg' } },
    },
    chatId: '15551234567@s.whatsapp.net',
    senderId: '15551234567@s.whatsapp.net',
    senderNumber: '15551234567',
    downloadMedia: async () => { throw new Error('Failed to fetch stream from https://mmg.whatsapp.net/x'); },
    cacheDirs: { image: mkdtempSync(path.join(tmpdir(), 'wa-media-')) },
  });
  assert.equal(event.hasMedia, true);
  assert.equal(event.mediaUrls.length, 0);
  assert.equal(event.body, '[image could not be downloaded]');
  console.log('  ✓ failed media download is contained and surfaced in body');
}

{
  // Captioned message keeps the caption and appends the failure note.
  const event = await extractBridgeEvent({
    msg: {
      key: { id: 'doc-fail-1', remoteJid: '15551234567@s.whatsapp.net', fromMe: false },
      messageTimestamp: 123,
      message: { documentMessage: { caption: 'see attached', fileName: 'q.pdf', mimetype: 'application/pdf' } },
    },
    chatId: '15551234567@s.whatsapp.net',
    senderId: '15551234567@s.whatsapp.net',
    senderNumber: '15551234567',
    downloadMedia: async () => { throw new Error('boom'); },
    cacheDirs: { document: mkdtempSync(path.join(tmpdir(), 'wa-media-')) },
  });
  assert.equal(event.body, 'see attached\n[document could not be downloaded]');
  assert.equal(event.mediaUrls.length, 0);
  console.log('  ✓ captioned failed download keeps caption and appends note');
}

console.log('\n✅ All WhatsApp native bridge helper tests passed.');
