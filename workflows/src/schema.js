// Runtime acceptance follows the dispatch schema, including nested and conditional constraints.
export function schemaAccepts(value, schema) {
  if (schema.type) {
    const types = Array.isArray(schema.type) ? schema.type : [schema.type];
    const actual = value === null ? 'null' : Array.isArray(value) ? 'array' : typeof value;
    if (!types.some((type) => type === 'integer' ? Number.isInteger(value) : type === 'number' ? typeof value === 'number' && Number.isFinite(value) : type === actual)) return false;
  }
  if (Object.hasOwn(schema, 'const') && value !== schema.const) return false;
  if (schema.enum && !schema.enum.includes(value)) return false;
  if (typeof value === 'string' && ((schema.minLength !== undefined && Array.from(value).length < schema.minLength)
    || (schema.maxLength !== undefined && Array.from(value).length > schema.maxLength)
    || (schema.pattern && !new RegExp(schema.pattern, 'u').test(value)))) return false;
  if (typeof value === 'number' && ((schema.minimum !== undefined && value < schema.minimum)
    || (schema.maximum !== undefined && value > schema.maximum))) return false;
  if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
    if (schema.required?.some((key) => !Object.hasOwn(value, key))) return false;
    for (const [key, item] of Object.entries(value)) {
      if (schema.properties && Object.hasOwn(schema.properties, key)) { if (!schemaAccepts(item, schema.properties[key])) return false; }
      else if (schema.additionalProperties === false) return false;
    }
  }
  if (Array.isArray(value) && schema.items && value.some((item) => !schemaAccepts(item, schema.items))) return false;
  if (schema.oneOf && schema.oneOf.filter((branch) => schemaAccepts(value, branch)).length !== 1) return false;
  if (schema.anyOf && !schema.anyOf.some((branch) => schemaAccepts(value, branch))) return false;
  if (schema.allOf && !schema.allOf.every((branch) => schemaAccepts(value, branch))) return false;
  if (schema.if) {
    const branch = schemaAccepts(value, schema.if) ? schema.then : schema.else;
    if (branch && !schemaAccepts(value, branch)) return false;
  }
  return true;
}
