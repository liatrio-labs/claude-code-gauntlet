// The same walker checks both readers and removed-test audits when the host omits validation.
const KEYWORDS = new Set(['type', 'const', 'enum', 'minLength', 'maxLength', 'pattern', 'minimum', 'maximum',
  'properties', 'required', 'additionalProperties', 'items', 'minItems', 'maxItems', 'uniqueItems',
  'oneOf', 'anyOf', 'allOf', 'not', 'if', 'then', 'else', 'description', '$comment', 'title', 'default', '$schema']);

export function schemaErrors(value, schema, options = {}) {
  const check = (value, schema, path) => {
    const fail = (keyword) => `${path}: ${keyword}`;
    if (schema === true) return null;
    if (schema === false) return fail('false schema');
    if (!schema || typeof schema !== 'object') return fail('invalid schema');
    const unknown = Object.keys(schema).find((key) => !KEYWORDS.has(key));
    if (unknown) return fail(`unsupported keyword ${unknown}`);
    if (schema.type) {
      const types = Array.isArray(schema.type) ? schema.type : [schema.type];
      const actual = value === null ? 'null' : Array.isArray(value) ? 'array' : typeof value;
      if (!types.some((type) => type === 'integer' ? Number.isSafeInteger(value) : type === 'number'
        ? typeof value === 'number' && Number.isFinite(value) : type === actual)) return fail('type');
    }
    if (Object.hasOwn(schema, 'const') && value !== schema.const) return fail('const');
    if (schema.enum && !schema.enum.includes(value)) return fail('enum');
    if (typeof value === 'string') {
      const length = Array.from(value).length;
      const nonblankLength = options.stringLength ? options.stringLength(value) : length;
      if (schema.minLength !== undefined && nonblankLength < schema.minLength) return fail('minLength');
      if (schema.maxLength !== undefined && length > schema.maxLength) return fail('maxLength');
      if (schema.pattern && !new RegExp(schema.pattern, 'u').test(value)) return fail('pattern');
    }
    if (typeof value === 'number') {
      if (schema.minimum !== undefined && value < schema.minimum) return fail('minimum');
      if (schema.maximum !== undefined && value > schema.maximum) return fail('maximum');
    }
    if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
      const missing = schema.required?.find((key) => !Object.hasOwn(value, key));
      if (missing) return `${path}.${missing}: required`;
      for (const [key, item] of Object.entries(value)) {
        const known = schema.properties && Object.hasOwn(schema.properties, key);
        // Unknown field names are untrusted prose; report the constraint without echoing them.
        const child = known ? schema.properties[key] : schema.additionalProperties;
        if (child === false) return fail('additionalProperties');
        if (child && typeof child === 'object') {
          const error = check(item, child, known ? `${path}.${key}` : `${path}.*`);
          if (error) return error;
        }
      }
    }
    if (Array.isArray(value)) {
      if (schema.minItems !== undefined && value.length < schema.minItems) return fail('minItems');
      if (schema.maxItems !== undefined && value.length > schema.maxItems) return fail('maxItems');
      if (schema.uniqueItems && new Set(value.map((item) => JSON.stringify(item))).size !== value.length) return fail('uniqueItems');
      if (schema.items !== undefined) for (const [index, item] of value.entries()) {
        const error = check(item, schema.items, `${path}[${index}]`);
        if (error) return error;
      }
    }
    for (const keyword of ['oneOf', 'anyOf', 'allOf']) if (schema[keyword]) {
      const branches = schema[keyword];
      const errors = branches.map((branch) => check(value, branch, path));
      const matches = errors.filter((error) => !error).length;
      if (keyword === 'allOf' && matches !== branches.length) return errors.find(Boolean);
      if ((keyword === 'oneOf' && matches !== 1) || (keyword === 'anyOf' && !matches)) {
        const selected = branches.findIndex((branch) => Object.entries(branch.properties || {}).some(([key, prop]) =>
          Object.hasOwn(prop, 'const') && value?.[key] === prop.const));
        return matches === 0 && selected >= 0 ? errors[selected] : fail(keyword);
      }
    }
    if (schema.not && !check(value, schema.not, path)) return fail('not');
    if (Object.hasOwn(schema, 'if')) {
      const branch = check(value, schema.if, path) ? schema.else : schema.then;
      if (branch !== undefined) { const error = check(value, branch, path); if (error) return error; }
    }
    return null;
  };
  const error = check(value, schema, '$');
  return error ? [error] : [];
}

export function schemaAccepts(value, schema, options) {
  return schemaErrors(value, schema, options).length === 0;
}
