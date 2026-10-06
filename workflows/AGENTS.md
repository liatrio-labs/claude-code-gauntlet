# workflows/

- `pipeline.js` is generated from `src/*.js` by `build.js`. Never hand-edit it; rebuild and commit
  it with the source change.
- The Workflow sandbox provides language globals plus the host-injected `agent`, `parallel`,
  `pipeline`, and `args`. It has no disk, shell, wall-clock, or Node and web globals. `node --test`
  supplies all of those, so a stray reference keeps every test green and throws on the first live
  dispatch. The lint gate is what catches it; `biome.json` lists the denied globals.
