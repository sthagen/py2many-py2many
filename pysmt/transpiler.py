import ast
from typing import List

from py2many.analysis import get_id, is_ellipsis, is_mutable, is_void_function
from py2many.declaration_extractor import DeclarationExtractor
from py2many.exceptions import AstNotImplementedError, AstTypeNotSupported
from py2many.tracer import defined_before

from .clike import CLikeTranspiler


class SmtTranspiler(CLikeTranspiler):
    NAME = "smt"

    def __init__(self, indent=2):
        super().__init__()
        self._indent = " " * indent
        CLikeTranspiler._default_type = "var"
        if "math" in self._ignored_module_set:
            self._ignored_module_set.remove("math")
        self._in_smt_pre = False

    def indent(self, code, level=1):
        return self._indent * level + code

    def usings(self):
        usings = sorted(list(set(self._usings)))
        uses = "\n".join(f"import {mod}" for mod in usings)
        return uses

    @classmethod
    def _combine_value_index(cls, value_type, index_type) -> str:
        return f"(_ {value_type} {index_type})"

    def comment(self, text):
        return f";; {text}\n"

    def _visit_DeclareFunc(self, node, return_type):
        return f"(declare-fun {node.name}() {return_type})"

    def visit_Attribute(self, node) -> str:
        attr = node.attr
        value_id = self.visit(node.value)

        if not value_id:
            value_id = ""

        type_decl = node.scopes.find(value_id)
        if type_decl is not None:
            decorators = [get_id(d) for d in type_decl.decorator_list]
            if "sealed" in decorators:
                # SMT prefers to use the datatype name directly
                return f"{attr}"

        return f"{value_id}.{attr}"

    def visit_FunctionDef(self, node):
        typenames, args = self.visit(node.args)

        args_list = []

        for i in range(len(args)):
            typename = typenames[i]
            arg = args[i]

            args_list.append(f"({arg} {typename})")

        return_type = ""
        if not is_void_function(node):
            if node.returns:
                typename = self._typename_from_annotation(node, attr="returns")
                return_type = f" {typename}"
            else:
                return_type = ""

        if len(node.body) == 1 and is_ellipsis(node.body[0]):
            return self._visit_DeclareFunc(node, return_type)

        args = " ".join(args_list)
        is_smt_pre = getattr(node, "is_smt_pre", False)
        suffix = "-pre" if is_smt_pre else ""
        fname = node.name.replace("_", "-")
        funcdef = f"define-fun {fname}({args}) {return_type}"
        body = "\n".join([self.indent(self.visit(n)) for n in node.body])
        if is_smt_pre:
            self._in_smt_pre = True
            # Only the first statement in the body is considered a pre-condition
            pre_body = "\n".join([self.indent(self.visit(n)) for n in node.body[:1]])
            self._in_smt_pre = False

            funcdef_pre = f"define-fun {fname}{suffix}({args}) Bool"
            return f"({funcdef_pre}\n{pre_body})\n\n({funcdef}\n{body})\n"
        else:
            return f"({funcdef}\n{body})\n"

    def visit_Return(self, node):
        if node.value:
            ret = self.visit(node.value)

            return f"{ret}"
        return ""

    def visit_arg(self, node):
        id = get_id(node)
        typename = "T"
        if node.annotation:
            typename = self._typename_from_annotation(node)
        return (typename, id)

    def _is_higher_order(self, node):
        return not (isinstance(node, ast.Name) or isinstance(node, ast.Attribute))

    def visit_Call(self, node):
        fname = self.visit(node.func)

        vargs = []

        if node.args:
            vargs += [self.visit(a) for a in node.args]
        if node.keywords:
            vargs += [self.visit(kw.value) for kw in node.keywords]

        ret = self._dispatch(node, fname, vargs)
        if ret is not None:
            return ret
        if vargs:
            args = " " + " ".join(vargs)
        else:
            args = ""
        if self._is_higher_order(node.func):
            return f"(apply {fname}{args})"
        ret = []
        fndef = node.scopes.find(fname)
        if fndef and getattr(fndef, "is_smt_pre", False):
            ret.append("(and")
            ret.append(f"({fname}-pre {args})")
            ret.append(f"({fname}{args})")
            ret.append(")")
        else:
            ret.append(f"({fname}{args})")
        return "\n".join(ret)

    def visit_sealed_class(self, node):
        variants = []
        complex = False
        for member, var in node.class_assignments.items():
            member_id = get_id(member)
            typename, default_val = node.declarations_with_defaults.get(member_id, None)
            if typename == self._default_type:
                variants.append("(None)")
            # Handle known builtin types
            elif typename in {"Callable", "Int", "Float", "Bool", "String"}:
                variants.append(member_id)
            else:
                complex = True
                innerv = []
                definition = node.scopes.parent_scopes.find(typename)
                if definition is None:
                    raise AstTypeNotSupported(f"{typename}", node)
                for member, var in definition.class_assignments.items():
                    member_id = get_id(member)
                    member_type = definition.declarations.get(member_id)
                    innerv.append(f"({member_id} {member_type})")
                innerv_str = f"{''.join(innerv)}"
                cons = typename.lower()
                variants.append(f"({cons} {innerv_str})")

        if not complex:
            variants_str = " ".join(variants)
            return f"(declare-datatypes () (({node.name} {variants_str})))"

        variants_str = f"({''.join(variants)})"
        return f"(declare-datatypes (({node.name} 0)) ({variants_str}))"

    def visit_ClassDef(self, node):
        extractor = DeclarationExtractor(SmtTranspiler())
        extractor.visit(node)
        declarations = node.declarations = extractor.get_declarations()
        node.declarations_with_defaults = extractor.get_declarations_with_defaults()
        node.class_assignments = extractor.class_assignments

        decorators = [get_id(d) for d in node.decorator_list]
        if "sealed" in decorators:
            # TODO: handle cases where sealed is stacked with other decorators
            return self.visit_sealed_class(node)

        fields = []
        index = 0
        for declaration, typename in declarations.items():
            if typename == None:
                typename = f"ST{index}"
                index += 1
            fields.append(f"{declaration}: {typename}")

        return ""

    def _import(self, name: str) -> str:
        return f"import {name}"

    def _import_from(self, module_name: str, names: List[str], level: int = 0) -> str:
        names = ", ".join(names)
        return f"from {module_name} import {names}"

    def visit_Subscript(self, node) -> str:
        value = self.visit(node.value)
        index = self.visit(node.slice)
        if (
            hasattr(node.value, "smt_annotation")
            and "Array" in node.value.smt_annotation
        ):
            return f"(select {value} {index})"
        return super().visit_Subscript(node)

    def visit_Tuple(self, node):
        elts = [self.visit(e) for e in node.elts]
        elts = " ".join(elts)
        if hasattr(node, "is_annotation"):
            return elts
        return f"({elts})"

    def visit_Assert(self, node):
        expr = self.visit(node.test)
        if self._in_smt_pre:
            return f"{expr}"
        return f"(assert {expr})"

    def visit_AnnAssign(self, node):
        target, type_str, val = super().visit_AnnAssign(node)
        if val == None or "default-value" in val:
            return f"(declare-const {target} {type_str})"
        else:
            raise AstNotImplementedError(f"{val} can't be assigned", node)

    def visit_Assign(self, node):
        lines = [self._visit_AssignOne(node, target) for target in node.targets]
        if len(lines) > 1:
            line0 = lines[0]
            lines = [self.indent(line, node.level) for line in lines]
            lines[0] = line0  # parent node is going to add indentation
        return "\n".join(lines)

    def visit_List(self, node) -> str:
        if not node.elts:
            value_type = "Int"  # Default to Int for empty arrays
            return f"((as const (Array Int {value_type})) 0)"

        elements = [self.visit(e) for e in node.elts]
        elements_str = " ".join(elements)

        # Infer element type from first element if possible
        first = node.elts[0]
        if hasattr(first, "smt_annotation"):
            value_type = first.smt_annotation
        else:
            value_type = "Int"  # Default

        indices = " ".join(str(i) for i in range(len(elements)))
        return f"((lambda ((i Int)) (ite (= i {indices}) {elements_str} 0)) :init)"

    def _visit_AssignOne(self, node, target):
        kw = "assert" if is_mutable(node.scopes, get_id(target)) else "let"

        if isinstance(target, ast.Subscript) or isinstance(target, ast.Attribute):
            target = self.visit(target)
            value = self.visit(node.value)
            return f"(assert (= {target} {value}))"

        definition = node.scopes.parent_scopes.find(get_id(target))
        if definition is None:
            definition = node.scopes.find(get_id(target))
        target = self.visit(target)
        if isinstance(target, ast.Name) and defined_before(definition, node):
            value = self.visit(node.value)
            return f"(assert (= {target} {value}))"
        elif isinstance(node.value, ast.List):
            values = self.visit(node.value)
            # parse a lisp list
            values = values[1:-1].split()
            return "\n".join(
                [
                    f"(assert (= (select {target} {i}) {val}))"
                    for i, val in enumerate(values)
                ]
            )
        else:
            value = self.visit(node.value)

            return f"({kw} ({target} {value}))"

    def visit_If(self, node, use_parens=True) -> str:
        buf = []
        check = get_id(node.test)
        if check == "smt_pre":
            if self._in_smt_pre:
                buf.append("(and")
                body = [self.visit(child) for child in node.body]
                body = [b for b in body if b is not None]
                buf.extend(body)
                buf.append(")")
            else:
                pass
        else:
            buf.append(f"(ite {self.visit(node.test)} ")
            body = [self.visit(child) for child in node.body]
            body = [b for b in body if b is not None]
            buf.extend(body)

            orelse = [self.visit(child) for child in node.orelse]
            if orelse:
                buf.extend(orelse)
                buf.append(")")
            else:
                buf.append("0)")
        return "\n".join(buf)
