// JSON.parse is the authority; this non-decoding scan locates faults when the host omits positions.
// An explicit stack keeps diagnosis independent of the input's nesting depth.
export function jsonSyntaxFault(text) {
  let offset = 0;
  const fault = (reason) => ({ offset, reason });
  const digit = (c) => c >= '0' && c <= '9';
  const string = () => {
    offset++;
    while (offset < text.length) {
      const c = text[offset++];
      if (c === '"') return null;
      if (c < ' ') { offset--; return fault('unescaped control character in JSON string'); }
      if (c !== '\\') continue;
      if (offset === text.length) return fault('incomplete escape in JSON string');
      const escape = text[offset++];
      if (escape === 'u') {
        for (let n = 0; n < 4; n++, offset++) {
          if (!/^[0-9a-fA-F]$/.test(text[offset] || '')) return fault('expected four hexadecimal digits in Unicode escape');
        }
      } else if (!'"\\/bfnrt'.includes(escape)) {
        offset--;
        return fault('invalid escape in JSON string');
      }
    }
    return fault('unterminated JSON string');
  };
  const number = () => {
    if (text[offset] === '-') offset++;
    if (!digit(text[offset])) return fault('expected a digit in JSON number');
    if (text[offset] === '0') {
      offset++;
      if (digit(text[offset])) return fault('leading zero in JSON number');
    } else while (digit(text[offset])) offset++;
    if (text[offset] === '.') {
      offset++;
      if (!digit(text[offset])) return fault('expected a digit after decimal point');
      while (digit(text[offset])) offset++;
    }
    if (text[offset] === 'e' || text[offset] === 'E') {
      offset++;
      if (text[offset] === '+' || text[offset] === '-') offset++;
      if (!digit(text[offset])) return fault('expected a digit in JSON exponent');
      while (digit(text[offset])) offset++;
    }
    return null;
  };
  const stack = [{ kind: 'root', state: 'value' }];
  for (;;) {
    while (offset < text.length && ' \t\r\n'.includes(text[offset])) offset++;
    const frame = stack[stack.length - 1];
    const c = text[offset];
    if (frame.state === 'done') return offset === text.length ? null : fault('unexpected data after the top-level JSON value');
    const close = frame.kind === 'object' ? '}' : ']';
    const emptyArray = frame.state === 'valueOrEnd';
    if (frame.state === 'keyOrEnd' || frame.state === 'valueOrEnd') {
      if (c === close) { offset++; stack.pop(); continue; }
      if (frame.state === 'keyOrEnd' && c !== '"') return fault("expected a quoted property name or '}'");
      frame.state = frame.kind === 'object' ? 'key' : 'value';
    }
    if (frame.state === 'key') {
      if (c !== '"') return fault('expected a quoted property name');
      const error = string();
      if (error) return error;
      frame.state = 'colon';
      continue;
    }
    if (frame.state === 'colon') {
      if (c !== ':') return fault("expected ':' after property name");
      offset++;
      frame.state = 'value';
      continue;
    }
    if (frame.state === 'comma') {
      if (c === close) { offset++; stack.pop(); continue; }
      if (c !== ',') return fault(`expected ',' or '${close}'`);
      offset++;
      frame.state = frame.kind === 'object' ? 'key' : 'value';
      continue;
    }
    frame.state = frame.kind === 'root' ? 'done' : 'comma';
    if (c === '{' || c === '[') {
      offset++;
      stack.push(c === '{' ? { kind: 'object', state: 'keyOrEnd' } : { kind: 'array', state: 'valueOrEnd' });
    } else if (c === '"' || c === '-' || digit(c)) {
      const error = c === '"' ? string() : number();
      if (error) return error;
    } else {
      const literal = ['true', 'false', 'null'].find((word) => word[0] === c);
      if (!literal) return fault(emptyArray ? "expected a JSON value or ']'" : 'expected a JSON value');
      for (const char of literal) {
        if (text[offset] !== char) return fault(`expected '${literal}'`);
        offset++;
      }
    }
  }
}
