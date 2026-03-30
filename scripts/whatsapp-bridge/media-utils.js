const MAX_WRAPPER_DEPTH = 8;

function unwrapOnce(content) {
  return (
    content?.ephemeralMessage?.message
    || content?.viewOnceMessage?.message
    || content?.viewOnceMessageV2?.message
    || content?.documentWithCaptionMessage?.message
    || null
  );
}

export function getMessageContent(msg) {
  let content = msg?.message || {};
  for (let depth = 0; depth < MAX_WRAPPER_DEPTH; depth += 1) {
    const nested = unwrapOnce(content);
    if (!nested || nested === content) break;
    content = nested;
  }
  if (content.templateMessage?.hydratedTemplate) return content.templateMessage.hydratedTemplate;
  if (content.buttonsMessage) return content.buttonsMessage;
  if (content.listMessage) return content.listMessage;
  return content;
}

export function getAudioMessage(msg) {
  const content = getMessageContent(msg);
  return content.audioMessage || content.pttMessage || null;
}
