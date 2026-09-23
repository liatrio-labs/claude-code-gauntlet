// Node title resolver matching fixtures; data only. kinds: js
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
def foreign_python(): pass
FOREIGN_PYTHON_CONSTANT = 1;
- id: foreign_yaml
test('mismatch", () => {});
	test	(	'tab title'	,	() => {});
~~~ { opener_key: 1 }
test('fenced title', () => {});
~~~
test('after fence', () => {});
```bad`info { invalid_key: 1 }
        ~~~ { deep_key: 1 }
test('deep fenced title', () => {});
        ~~~
test('after deep fence', () => {});
