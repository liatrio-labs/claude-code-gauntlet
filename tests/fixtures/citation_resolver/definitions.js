// JavaScript resolver matching fixtures; data only.
function js_ok() {}
// function js_inline() {}
function js_prefix_long() {}
functionJoined();
class JsOk {}
// class JsInline {}
class JsPrefixLong {}
const VAR_OK = 1;
let LET_OK = 1;
var OLD_OK = 1;
// const VAR_INLINE = 1;
const VAR_LONG = 1;
else var_fake = 1;
const OBJECT = { key_ok: 1, "double_key": 2, 'single_key': 3 };
x ? key_inline : 0;
key_call();
const KEYS = { key_long: 1 };
FOREIGN_PYTHON_CONSTANT = 1;
const SAME = { same_key: 1 };
const $cash = 1;
const EQUALITY == 1;
export default async function* js_star() {}
export default class JsExport {}
classJoinedClass {}
export const EXP_OK = 1;
constJoinedVar = 1;
exportfunction export_joined() {}
defaultfunction default_joined() {}
asyncfunction async_joined() {}
exportclass ExportJoined {}
defaultclass DefaultJoined {}
exportconst EXPORT_JOINED = 1;
exportXfunction export_punct() {}
defaultXfunction default_punct() {}
asyncXfunction async_punct() {}
functionX fn_punct() {}
exportXclass ExportPunct {}
defaultXclass DefaultPunct {}
exportXconst EXPORT_PUNCT = 1;
start_key: 1;
def foreign_python(): pass
- id: foreign_yaml
