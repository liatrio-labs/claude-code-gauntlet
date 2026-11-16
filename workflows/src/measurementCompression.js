const BASE64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';

// ASCII JSON keeps the transport independent of UTF-16 code units and host byte APIs.
export function compressMeasurement(text) {
  const strings = [], indices = new Map();
  const pack = (value) => {
    if (typeof value === 'string') {
      if (!indices.has(value)) { indices.set(value, strings.length); strings.push(value); }
      return [0, indices.get(value)];
    }
    if (Array.isArray(value)) return [1, value.map(pack)];
    if (value !== null && typeof value === 'object') return [2, Object.entries(value).map(([key, item]) => [key, pack(item)])];
    return value;
  };
  // Intern complete strings before dictionary coding so repeated verdict prose has no window limit.
  const root = pack(JSON.parse(text));
  const input = JSON.stringify({ strings, root }).replace(/[\u007f-\uffff]/g,
    (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, '0')}`);
  const dictionary = new Map();
  let nextCode = 256;
  const bytes = [];
  const emit = (code) => bytes.push(code >>> 8, code & 255);
  let word = input.charCodeAt(0);
  for (let i = 1; i < input.length; i++) {
    const char = input.charCodeAt(i), pair = word * 256 + char;
    const code = dictionary.get(pair);
    if (code !== undefined) word = code;
    else {
      emit(word);
      if (nextCode < 65536) dictionary.set(pair, nextCode++);
      word = char;
    }
  }
  if (input.length) emit(word);
  const encoded = [];
  for (let i = 0; i < bytes.length; i += 3) {
    const a = bytes[i], b = bytes[i + 1], c = bytes[i + 2];
    encoded.push(BASE64[a >>> 2], BASE64[((a & 3) << 4) | ((b ?? 0) >>> 4)],
      b === undefined ? '=' : BASE64[((b & 15) << 2) | ((c ?? 0) >>> 6)], c === undefined ? '=' : BASE64[c & 63]);
  }
  return encoded.join('');
}
