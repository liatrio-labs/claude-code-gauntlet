// shellWords.js — read a pinned executor command the way a POSIX shell would.
//
// The pinned commands (verifyCommand, assemblePrompt) are strings that Bash will split
// into argv. Asserting "the path survived" by regex-matching the string proves nothing:
// `--diff-file /My Documents/d.patch` matches every substring test and still reaches the
// script as two arguments. So the issue-#75 tests assert on the ARGV a shell would build.
//
// This is a deliberate SUBSET of the shell's word-splitting grammar — single quotes,
// backslash escapes, and unquoted blanks — because that is the whole of what the pinned
// commands may contain. Anything richer (a double quote, an unterminated quote) throws
// rather than being silently tolerated: a command that needs more than this subset has
// already left the AST-safe emission contract.

// shellSplit(cmd) -> the argv array a POSIX shell would pass to execve.
export function shellSplit(cmd) {
  const words = [];
  let cur = null; // null = between words; '' = inside a word that is still empty
  let i = 0;
  while (i < cmd.length) {
    const ch = cmd[i];
    if (ch === ' ' || ch === '\t') {
      if (cur !== null) { words.push(cur); cur = null; }
      i += 1;
      continue;
    }
    if (cur === null) cur = '';
    if (ch === "'") {
      const end = cmd.indexOf("'", i + 1);
      if (end === -1) throw new Error(`unterminated single quote: ${cmd}`);
      cur += cmd.slice(i + 1, end);
      i = end + 1;
      continue;
    }
    if (ch === '\\') {
      if (i + 1 >= cmd.length) throw new Error(`trailing backslash: ${cmd}`);
      cur += cmd[i + 1];
      i += 2;
      continue;
    }
    if (ch === '"') throw new Error(`unexpected double quote: ${cmd}`);
    cur += ch;
    i += 1;
  }
  if (cur !== null) words.push(cur);
  return words;
}

// outsideSingleQuotes(cmd) -> everything NOT inside a single-quoted run, quotes dropped.
// A `$` or a backtick here would be live shell syntax; the same character inside a
// single-quoted run is an ordinary byte. Tests scan this, never the raw command.
export function outsideSingleQuotes(cmd) {
  let out = '';
  let i = 0;
  while (i < cmd.length) {
    if (cmd[i] === "'") {
      const end = cmd.indexOf("'", i + 1);
      if (end === -1) throw new Error(`unterminated single quote: ${cmd}`);
      i = end + 1;
      continue;
    }
    out += cmd[i];
    i += 1;
  }
  return out;
}
