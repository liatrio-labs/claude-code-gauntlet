// Dispatch definitions are the inventory used by execution and schema audits.
export const AGENT_CALLS = [];
const ROOT_COMPOSITION = ['oneOf', 'allOf', 'anyOf'];

export function toolSchemaErrors(schema) {
  const errors = [];
  if (!schema || schema.type !== 'object' || !schema.properties) errors.push('root must be an object with properties');
  for (const key of ROOT_COMPOSITION) if (schema && Object.hasOwn(schema, key)) errors.push(`root ${key} is unsupported`);
  const visit = (node, path) => {
    if (!node || typeof node !== 'object') return;
    if (node.type === 'array' && !node.items) errors.push(`${path}: array lacks items`);
    if (node.type === 'object' && !node.properties) errors.push(`${path}: object lacks properties`);
    if (node.enum && (!Array.isArray(node.enum) || !node.enum.length)) errors.push(`${path}: enum must be nonempty`);
    for (const [key, value] of Object.entries(node.properties || {})) visit(value, `${path}.${key}`);
    if (node.items) visit(node.items, `${path}[]`);
    for (const key of ROOT_COMPOSITION) for (const child of node[key] || []) visit(child, `${path}.${key}`);
    for (const key of ['if', 'then', 'else', 'not']) if (node[key]) visit(node[key], `${path}.${key}`);
  };
  visit(schema, '$');
  return errors;
}

export function defineAgentCall(name, schemaFor, variants = () => [[]]) {
  if (AGENT_CALLS.some((call) => call.name === name)) throw new Error(`Duplicate agent call ${name}`);
  AGENT_CALLS.push({ name, schemaFor, variants });
  return async (ctx, prompt, options, ...inputs) => {
    const request = { stage: name === 'refutations' ? options.label.split('-')[0] : name.split(':')[0], error: null };
    if (ctx.agentRequests) ctx.agentRequests.push(request);
    try {
      const schema = schemaFor(...inputs);
      const errors = toolSchemaErrors(schema);
      if (errors.length) throw new Error(`Invalid agent input_schema for ${name}: ${errors.join('; ')}`);
      return await ctx.agent(prompt, { ...options, schema });
    } catch (error) {
      request.error = String(error?.message ?? error).replace(/\s+/g, ' ').trim();
      throw error;
    }
  };
}

export function repeatedRequestGap(requests, stage) {
  const calls = requests.filter((request) => request.stage === stage);
  const error = calls[0]?.error;
  return calls.length && error && /API Error:\s*400\b|invalid_request_error|input_schema/i.test(error)
    && calls.every((request) => request.error === error)
    ? `${stage}: all ${calls.length} agent requests failed with the same request error: ${error}` : null;
}
