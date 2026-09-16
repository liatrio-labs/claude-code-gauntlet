# Python resolver matching fixtures; data only. kinds: python, python_constant
def py_ok(): pass
# def py_inline(): pass
def py_prefix_long(): pass
defjoined()
class PyOk: pass
# class PyInline: pass
class PyPrefixLong: pass
UPPER_OK = 1
ANNOTATED_OK: int = 1
UPPER_LONG = 1
ANNOTATION: a == b
COMPARE == 1
lower = 1
Mixed = 1
def scope():
    INDENTED = 1
foreign_js: 1
def early(): pass
class Parent:
    def child(self): pass
class Parent: pass
async def py_async(): pass
classJoined:
asyncdef async_joined(): pass
EMPTY:=1
asyncXdef async_punct(): pass
- id: foreign_yaml
class WithBase(Base): pass
	async	def	tab_def	(): pass
	class	TabClass	:
TAB_CONST	=	1
