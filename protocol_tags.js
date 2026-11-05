/* Readable message wrappers only; preserve code samples and arbitrary HTML/XML. */
function stripAgentTags(value) {
  const wrappers = new Set(['user_request', 'user_message', 'user_prompt', 'assistant_response', 'final_answer']);
  const source = String(value == null ? '' : value), out = [];
  let i = 0, code = null;
  while (i < source.length) {
    const run = source.slice(i).match(/^(`+|~{3,})/);
    if (run && (run[1][0] === '`' || /(^|\n) {0,3}$/.test(source.slice(0, i)))) {
      const mark = run[1];
      if (!code) code = mark;
      else if (code[0] === mark[0] && (code.length < 3 ? code.length === mark.length : mark.length >= code.length)) code = null;
      out.push(mark);
      i += mark.length;
      continue;
    }
    if (!code && source[i] === '<') {
      const tag = source.slice(i).match(/^<\s*\/?\s*([a-z_][a-z0-9_-]*)(?:\s+[^<>]*?)?\s*\/?>/i);
      if (tag && wrappers.has(tag[1].toLowerCase())) {
        i += tag[0].length;
        continue;
      }
      // Withhold a recognized delimiter suffix while a streaming repaint awaits
      // the rest. A short '<u' remains literal to avoid hiding normal prose.
      const partial = source.slice(i).match(/^<\s*\/?\s*([a-z_][a-z0-9_-]*)$/i);
      if (partial && partial[1].length >= 3 && [...wrappers].some(name => name.startsWith(partial[1].toLowerCase()))) break;
      const attributes = source.slice(i).match(/^<\s*\/?\s*([a-z_][a-z0-9_-]*)\s+[^<>]*$/i);
      if (attributes && wrappers.has(attributes[1].toLowerCase())) break;
    }
    out.push(source[i++]);
  }
  return out.join('');
}
if (typeof module !== 'undefined' && module.exports) module.exports = {stripAgentTags};
