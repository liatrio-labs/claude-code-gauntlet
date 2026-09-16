// Node title resolver matching fixtures; data only.
test('single title', () => {});
describe("double title", () => {});
it(`plain template`, () => {});
helper('wrong callee', () => {});
test('long title suffix', () => {});
test('joined title' + suffix, () => {});
object.test('inline title', () => {});
test('it\'s escaped', () => {});
test('path\\part', () => {});
test('literal (dot.)', () => {});
test(`dynamic ${name}`, () => {});
test('literal ${name}', () => {});
describe("say \"yes\"", () => {});
it(`tick \`value\``, () => {});
testX('callee gap', () => {});
test(X'argument gap', () => {});
