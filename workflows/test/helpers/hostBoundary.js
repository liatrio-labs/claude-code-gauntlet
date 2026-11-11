// Host boundaries copy data, so Node fixtures must not preserve collections or prototypes.
export async function hostResult(value) {
  const result = await value;
  return result === undefined ? null : JSON.parse(JSON.stringify(result));
}
