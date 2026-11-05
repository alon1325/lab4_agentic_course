/* Readable-view filtering only: raw requests and forwarded bytes stay untouched. */
const AGENT_CONTEXT_TAGS = new Set([
  'environment_context', 'environment-context', 'system-reminder', 'system_reminder',
  'conversation_context', 'conversation-context', 'context', 'injected_context',
  'developer', 'developer_instructions', 'developer-instructions',
  'system', 'system_instructions', 'system-instructions', 'instructions',
  'permissions', 'permissions_instructions', 'permissions-instructions',
  'collaboration_mode', 'collaboration-mode', 'turn_aborted',
  'user-prompt-submit-hook', 'local-command-caveat',
]);
function isAgentContextTag(name) {
  name = name.toLowerCase();
  return AGENT_CONTEXT_TAGS.has(name) || /^ide_[a-z_]+$/.test(name) || /^local-command-[a-z-]+$/.test(name);
}

function splitAgentContext(value) {
  const source = String(value == null ? '' : value);
  const visible = [], contexts = [], stack = [];
  let i = 0, code = null, root = null;
  const append = text => { if (root) root.text += text; else visible.push(text); };
  const finish = () => {
    if (root) contexts.push({tag: root.tag, text: root.text.trim()});
    root = null;
    stack.length = 0;
  };
  while (i < source.length) {
    // Preserve code examples verbatim, including literal protocol-looking tags.
    const tick = source.slice(i).match(/^(`+|~{3,})/);
    if (tick && (tick[1][0] === '`' || /(^|\n) {0,3}$/.test(source.slice(0, i)))) {
      const mark = tick[1];
      if (!code) code = mark;
      else if (code[0] === mark[0] && (code.length < 3 ? code.length === mark.length : mark.length >= code.length)) code = null;
      append(mark);
      i += mark.length;
      continue;
    }
    if (!code && source[i] === '<') {
      const tag = source.slice(i).match(/^<\s*(\/?)\s*([a-z][a-z0-9_-]*)(?:\s+[^<>]*?)?\s*(\/?)>/i);
      if (tag && isAgentContextTag(tag[2])) {
        const name = tag[2].toLowerCase();
        if (!tag[1]) {
          if (!root) root = {tag: name, text: ''};
          if (!tag[3]) stack.push(name);
          else if (!stack.length) finish();
        } else {
          const at = stack.lastIndexOf(name);
          if (at >= 0) {
            stack.length = at;
            if (!stack.length) finish();
          }
          // A trailing known closing delimiter is protocol noise, not user text.
        }
        i += tag[0].length;
        continue;
      }
      // A stream may currently end halfway through a known opening delimiter.
      // Withhold that suffix until a subsequent repaint completes the block.
      const partial = source.slice(i).match(/^<\s*\/?\s*([a-z][a-z0-9_-]*)$/i);
      if (partial && partial[1].length >= 3 && [...AGENT_CONTEXT_TAGS].some(name => name.startsWith(partial[1].toLowerCase()))) {
        if (!root) root = {tag: partial[1].toLowerCase(), text: ''};
        i = source.length;
        break;
      }
      // Also handle an opening tag whose attributes have not finished streaming.
      const unfinished = source.slice(i).match(/^<\s*([a-z][a-z0-9_-]*)\s+[^<>]*$/i);
      if (unfinished && isAgentContextTag(unfinished[1])) {
        if (!root) root = {tag: unfinished[1].toLowerCase(), text: ''};
        i = source.length;
        break;
      }
    }
    append(source[i++]);
  }
  // Unclosed context blocks remain hidden; their contents may still be streaming.
  finish();
  return {text: visible.join('').trim(), contexts};
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {splitAgentContext, isAgentContextTag};
}
