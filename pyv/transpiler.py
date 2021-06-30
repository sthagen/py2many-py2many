import ast
import string
from typing import List

from py2many.tracer import is_list, defined_before
from py2many.exceptions import AstNotImplementedError
from py2many.analysis import get_id, is_mutable, is_void_function

from .clike import CLikeTranspiler
from .inference import V_WIDTH_RANK
from .plugins import (
    ATTR_DISPATCH_TABLE,
    FUNC_DISPATCH_TABLE,
    DISPATCH_MAP,
    SMALL_DISPATCH_MAP,
    SMALL_USINGS_MAP,
)

_is_mutable = is_mutable


def is_mutable(scopes, target):
    if target == "_":
        return False
    return _is_mutable(scopes, target)


def is_dict(node):
    if isinstance(node, (ast.Dict, ast.DictComp)):
        return True
    elif isinstance(node, ast.Call) and get_id(node.func) == "dict":
        return True
    elif isinstance(node, ast.Assign):
        return is_dict(node.value)
    elif isinstance(node, ast.Name):
        var = node.scopes.find(get_id(node))
        return (
            hasattr(var, "assigned_from")
            and not isinstance(var.assigned_from, ast.FunctionDef)
            and not isinstance(var.assigned_from, ast.For)
            and is_dict(var.assigned_from.value)
        )
    else:
        return False


class VDictRewriter(ast.NodeTransformer):
    def visit_Call(self, node):
        if (
            isinstance(node.func, ast.Attribute) and node.func.attr == "values"
        ):  # and is_dict(node.func.value):
            new_node = ast.parse("a.keys().map(a[it])").body[0].value
            new_node.func.value.func.value = node.func.value
            new_node.args[0].value = node.func.value
            new_node.lineno = node.lineno
            new_node.col_offset = node.col_offset
            ast.fix_missing_locations(new_node)
            return new_node
        return node


class VNoneCompareRewriter(ast.NodeTransformer):
    def visit_Compare(self, node):
        left = self.visit(node.left)
        right = self.visit(node.comparators[0])
        if (
            isinstance(right, ast.Constant)
            and right.value is None
            and isinstance(left, ast.Constant)
            and isinstance(left.value, int)
        ):
            # Convert None to 0 to compare vs int
            right.value = 0
        return node


class VTranspiler(CLikeTranspiler):
    NAME = "v"

    CONTAINER_TYPE_MAP = {
        "List": "[]",
        "Dict": "map",
        "Set": "set",
        "Optional": "?",
    }

    ALLOW_MODULE_LIST = ["math"]

    def __init__(self, indent=2):
        super().__init__()
        self._headers = set([])
        self._indent = " " * indent
        self._default_type = "any"
        self._container_type_map = self.CONTAINER_TYPE_MAP
        self._dispatch_map = DISPATCH_MAP
        self._small_dispatch_map = SMALL_DISPATCH_MAP
        self._small_usings_map = SMALL_USINGS_MAP
        self._func_dispatch_table = FUNC_DISPATCH_TABLE
        self._attr_dispatch_table = ATTR_DISPATCH_TABLE

    def indent(self, code, level=1):
        return self._indent * level + code

    def usings(self):
        usings = sorted(list(set(self._usings)))
        uses = "\n".join(f"import {mod}" for mod in usings)
        return uses

    def _combine_value_index(self, value_type, index_type) -> str:
        return f"{value_type}{index_type}"

    def comment(self, text):
        return f"// {text}\n"

    def _import(self, name: str) -> str:
        if name == "sys":
            return "import os"
        return f"import {name}"

    def _import_from(self, module_name: str, names: List[str]) -> str:
        return f"import {module_name} {{{' '.join(names)}}}"

    def function_signature(self, node) -> str:
        signature = ["fn"]
        if node.scopes[-1] is ast.ClassDef:
            raise AstNotImplementedError("Class methods are not supported yet.", node)

        if name := get_id(node):
            signature.append(name)

        args = []
        generic_count = 0
        for arg in node.args.args:
            typename = string.ascii_uppercase[generic_count]
            id = get_id(arg)
            if is_mutable(node.scopes, id):
                id = f"mut {id}"
            if getattr(arg, "annotation", None):
                typename = self._typename_from_annotation(arg, attr="annotation")
            if len(typename) == 1 and typename.isupper():
                generic_count += 1
            args.append(f"{id} {typename}")

        if generic_count:
            signature.append(f"<{', '.join(string.ascii_uppercase[:generic_count])}>")

        signature.append(f"({', '.join(args)})")
        if isinstance(node, ast.Lambda):
            if getattr(node, "annotation", None):
                typename = self._typename_from_annotation(node, attr="annotation")
                signature.append(typename)
        elif not is_void_function(node):
            if getattr(node, "returns", None):
                typename = self._typename_from_annotation(node, attr="returns")
                signature.append(typename)

        return " ".join(signature)

    def visit_FunctionDef(self, node):
        body = "\n".join([self.indent(self.visit(n)) for n in node.body])
        return f"{self.function_signature(node)} {{\n{body}\n}}"

    def visit_Return(self, node):
        if node.value:
            ret = self.visit(node.value)
            return f"return {ret}"
        return "return"

        if node.value:
            return "return {0}".format(self.visit(node.value))
        return "return"

    def visit_Lambda(self, node):
        raise AstNotImplementedError("Lambdas are not supported yet.", node)

    def visit_Attribute(self, node):
        attr = node.attr

        value_id = self.visit(node.value)

        if is_list(node.value):
            if node.attr == "append":
                return f"{value_id} <<"
        if not value_id:
            value_id = ""
        ret = f"{value_id}.{attr}"
        if ret in self._attr_dispatch_table:
            return self._attr_dispatch_table[ret](self, node)
        return value_id + "." + attr

    def _visit_object_literal(self, node, fname: str, fndef: ast.ClassDef):
        vargs = []  # visited args
        if not hasattr(fndef, "declarations"):
            raise Exception("Missing declarations")
        if node.args:
            for arg, decl in zip(node.args, fndef.declaration.keys()):
                arg = self.visit(arg)
                vargs += [f"{decl}: {arg}"]
        if node.keywords:
            for kw in node.keywords:
                value = self.visit(kw.value)
                vargs += [f"{kw.arg}: {value}"]
        args = ", ".join(vargs)
        return f"{fname}({args})"

    def visit_Call(self, node):
        fname = self.visit(node.func)
        fndef = node.scopes.find(fname)

        if isinstance(fndef, ast.ClassDef):
            return self._visit_object_literal(node, fname, fndef)

        vargs = []

        if node.args:
            vargs.extend(map(self.visit, node.args))
        if node.keywords:
            vargs += [self.visit(kw.value) for kw in node.keywords]

        ret = self._dispatch(node, fname, vargs)
        if ret is not None:
            return ret
        if vargs:
            args = ", ".join(vargs)
        else:
            args = ""
        return f"{fname}({args})"

    def visit_For(self, node):
        target = self.visit(node.target)
        buf = []
        if (
            isinstance(node.iter, ast.Call)
            and get_id(node.iter.func) == "range"
            and len(node.iter.args) == 3
        ):
            start = self.visit(node.iter.args[0])
            end = self.visit(node.iter.args[1])
            step = self.visit(node.iter.args[2])
            buf.append(
                f"for {target} := {start}; {target} < {end}; {target} += {step} {{"
            )
        else:
            it = self.visit(node.iter)
            buf.append(f"for {target} in {it} {{")
        buf.extend(
            [self.indent(self.visit(c), level=node.level + 1) for c in node.body]
        )
        buf.append("}")
        return "\n".join(buf)

    def visit_While(self, node):
        buf = []
        if isinstance(node.test, ast.Constant) and node.test.value == True:
            buf.append("for {")
        else:
            buf.append(f"for {self.visit(node.test)} {{")
        buf.extend(
            [self.indent(self.visit(n), level=node.level + 1) for n in node.body]
        )
        buf.append("}")
        return "\n".join(buf)

    def visit_Str(self, node):
        return super().visit_Str(node)

    def visit_Bytes(self, node):
        if not node.s:
            return "[]byte{}"

        chars = []
        chars.append(f"byte({hex(node.s[0])})")
        for c in node.s[1:]:
            chars.append(hex(c))
        return f"[{', '.join(chars)}]"

    def visit_If(self, node):
        body_vars = set([get_id(v) for v in node.scopes[-1].body_vars])
        orelse_vars = set([get_id(v) for v in node.scopes[-1].orelse_vars])
        node.common_vars = body_vars.intersection(orelse_vars)

        body = "\n".join(
            [
                self.indent(self.visit(child), level=node.level + 1)
                for child in node.body
            ]
        )
        orelse = "\n".join(
            [
                self.indent(self.visit(child), level=node.level + 1)
                for child in node.orelse
            ]
        )
        test = self.visit(node.test)
        if node.orelse:
            orelse = self.indent(f"else {{\n{orelse}\n}}", level=node.level)
        else:
            orelse = ""
        return f"if {test} {{\n{body}\n}}\n{orelse}"

    def visit_UnaryOp(self, node):
        if isinstance(node.op, ast.USub):
            if isinstance(node.operand, (ast.Call, ast.Num)):
                # Shortcut if parenthesis are not needed
                return "-{0}".format(self.visit(node.operand))
            else:
                return "-({0})".format(self.visit(node.operand))
        else:
            return super().visit_UnaryOp(node)

    def visit_ClassDef(self, node):
        raise AstNotImplementedError("Classes are not supported yet.", node)

    def visit_IntEnum(self, node):
        raise AstNotImplementedError("Enums are not supported yet.", node)

    def visit_IntFlag(self, node):
        raise AstNotImplementedError("Enums are not supported yet.", node)

    def visit_StrEnum(self, node):
        raise AstNotImplementedError("String enums are not supported in V.", node)

    def visit_List(self, node):
        elements = [self.visit(e) for e in node.elts]
        elements = ", ".join(elements)
        return f"[{elements}]"

    def visit_Set(self, node):
        raise AstNotImplementedError("Sets are not implemented in V yet.", node)

    def visit_Dict(self, node):
        keys = [self.visit(k) for k in node.keys]
        values = [self.visit(k) for k in node.values]
        kv_pairs = " ".join([f"{k}: {v}" for k, v in zip(keys, values)])
        return f"map{{{kv_pairs}}}"

    def visit_Subscript(self, node):
        value = self.visit(node.value)
        index = self.visit(node.slice)
        if hasattr(node, "is_annotation"):
            if value in self.CONTAINER_TYPE_MAP:
                value = self.CONTAINER_TYPE_MAP[value]
            if value == "Tuple":
                return f"({index})"
            return f"{value}[{index}]"
        return f"{value}[{index}]"

    def visit_Index(self, node):
        return self.visit(node.value)

    def visit_Slice(self, node):
        lower = ""
        if node.lower:
            lower = self.visit(node.lower)
        upper = ""
        if node.upper:
            upper = self.visit(node.upper)

        return "{0}..{1}".format(lower, upper)

    def visit_Elipsis(self, node):
        return ""

    def visit_Tuple(self, node):
        elts = [self.visit(e) for e in node.elts]
        elts = ", ".join(elts)
        if hasattr(node, "is_annotation"):
            return elts
        return "({0})".format(elts)

    def visit_Try(self, node, finallybody=None):
        raise AstNotImplementedError("Exceptions are not supported yet.", node)

    def visit_ExceptHandler(self, node):
        raise AstNotImplementedError("Exceptions are not supported yet.", node)

    def visit_Assert(self, node):
        return f"assert {self.visit(node.test)}"

    def visit_AnnAssign(self, node):
        target, type_str, val = super().visit_AnnAssign(node)
        kw = "mut " if is_mutable(node.scopes, target) else ""
        if isinstance(node.value, ast.List):
            if node.value.elts:
                elts = []
                if type_str[2:] in V_WIDTH_RANK:
                    elts.append(f"{type_str[2:]}({self.visit(node.value.elts[0])})")
                else:
                    elts.append(self.visit(node.value.elts[0]))
                elts.extend(map(self.visit, node.value.elts[1:]))
                return f"{kw}{target} := [{', '.join(elts)}]"
            return f"{kw}{target} := {type_str}{{}}"
        else:
            return f"{kw}{target} := {val}"

    def visit_Assign(self, node):
        assign = []
        use_temp = len(node.targets) > 1 and isinstance(node.value, ast.Call)
        if use_temp:
            assign.append(f"mut tmp := {self.visit(node.value)}")
        for target in node.targets:
            kw = "mut " if is_mutable(node.scopes, get_id(target)) else ""
            if use_temp:
                value = "tmp"
            else:
                value = self.visit(node.value)

            if isinstance(target, (ast.Tuple, ast.List)):
                value = value[1:-1]
                subtargets = []
                op = ":="
                for subtarget in target.elts:
                    subkw = "mut " if is_mutable(node.scopes, get_id(subtarget)) else ""
                    subtargets.append(f"{subkw}{self.visit(subtarget)}")
                    definition = node.scopes.find(get_id(subtarget))
                    if definition is not None and defined_before(definition, subtarget):
                        op = "="
                    elif op == "=":
                        raise AstNotImplementedError(
                            "Mixing declarations and assignment in the same statement is unsupported.",
                            node,
                        )
                assign.append(f"{', '.join(subtargets)} {op} {value}")
            elif isinstance(target, (ast.Subscript, ast.Attribute)):
                target = self.visit(target)
                assign.append(f"{target} = {value}")
            elif isinstance(target, ast.Name) and defined_before(
                node.scopes.find(target.id), node
            ):
                target = self.visit(target)
                assign.append(f"{target} = {value}")
            else:
                target = self.visit(target)

                assign.append(f"{kw}{target} := {value}")
        return "\n".join(assign)

    def visit_AugAssign(self, node):
        target = self.visit(node.target)
        op = self.visit(node.op)
        val = self.visit(node.value)
        return "{0} {1}= {2}".format(target, op, val)

    def visit_Delete(self, node):
        raise AstNotImplementedError("`delete` statements are not supported yet.", node)

    def visit_Raise(self, node):
        raise AstNotImplementedError("Exceptions are not supported yet.", node)

    def visit_With(self, node):
        raise AstNotImplementedError("`with` statements are not supported yet.", node)

    def visit_Await(self, node):
        raise AstNotImplementedError("asyncio is not supported.", node)

    def visit_AsyncFunctionDef(self, node):
        raise AstNotImplementedError("asyncio is not supported.", node)

    def visit_Yield(self, node):
        raise AstNotImplementedError("Generators are not supported yet.", node)

    def visit_DictComp(self, node):
        raise AstNotImplementedError("Dict comprehensions are not supported yet.", node)

    def visit_GeneratorExp(self, node):
        raise AstNotImplementedError(
            "Generator expressions are not supported yet.", node
        )

    def visit_ListComp(self, node):
        return self.visit_GeneratorExp(node)  # right now they are the same

    def visit_Global(self, node):
        raise AstNotImplementedError("Globals are not supported yet.", node)

    def visit_Starred(self, node):
        raise AstNotImplementedError("Starred expressions are not supported yet.", node)

    def visit_IfExp(self, node):
        body = self.visit(node.body)
        orelse = self.visit(node.orelse)
        test = self.visit(node.test)
        return f"if {test} {{ {body} }} else {{ {orelse} }}"