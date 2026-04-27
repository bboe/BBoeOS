"""Recursive-descent parser for the C subset grammar.

Consumes the token stream produced by :mod:`cc.lexer` / processed by
:mod:`cc.preprocessor` and builds an AST using the node dataclasses in
:mod:`cc.ast_nodes`.
"""

from __future__ import annotations

from cc.ast_nodes import (
    AddressOf,
    ArrayDecl,
    ArrayInit,
    Assign,
    BinaryOperation,
    Break,
    Call,
    Char,
    Continue,
    DerefAssign,
    DoWhile,
    Function,
    If,
    Index,
    IndexAssign,
    InlineAsm,
    Int,
    LogicalAnd,
    LogicalOr,
    MemberAccess,
    MemberAssign,
    MemberIndex,
    Node,
    Param,
    Program,
    Return,
    SizeofType,
    SizeofVar,
    String,
    StructDecl,
    StructField,
    StructInit,
    TailCall,
    Var,
    VarDecl,
    While,
)
from cc.errors import CompileError
from cc.tokens import (
    ADDITIVE_OPERATORS,
    COMPARISON_OPERATIONS,
    COMPARISON_OPERATORS,
    COMPOUND_ASSIGN_OPERATORS,
    MULTIPLICATIVE_OPERATORS,
    SHIFT_OPERATORS,
    TYPE_TOKENS,
)
from cc.utils import decode_first_character


class Parser:
    """Recursive descent parser for the C subset grammar."""

    def __init__(self, tokens: list[tuple[str, str, int]], /) -> None:
        """Initialize the parser with a token list."""
        self.tokens = tokens
        self.position = 0
        self.struct_decls: dict[str, StructDecl] = {}

    def eat(self, kind: str | None = None) -> tuple[str, str, int]:
        """Consume and return the current token, optionally checking its kind.

        Returns:
            The consumed token as a (kind, text, line) triple.

        Raises:
            CompileError: If the token kind does not match the expected kind.

        """
        token = self.tokens[self.position]
        if kind is not None and token[0] != kind:
            message = f"expected {kind}, got {token[0]} ({token[1]!r})"
            raise CompileError(message, line=token[2])
        self.position += 1
        return token

    @staticmethod
    def fold_binop(operator: str, left: Node, right: Node, /) -> Node:
        """Return a folded node when operands (or a left-subtree tail) are constant.

        Handles two shapes:

        1. ``Int operation Int`` collapses to a single ``Int`` — lets
           ``COLUMNS - 1`` become ``39`` at parse time.
        2. ``(X op1 Int1) op2 Int2`` with ``op1, op2`` both additive
           folds the trailing constants through so
           ``(column + 40) - 1`` becomes ``column + 39`` and
           ``(column + 1) % 40`` keeps the ``%`` outer but the inner
           addition is already a tight pair.
        """
        line = left.line
        if isinstance(left, Int) and isinstance(right, Int):
            a, b = left.value, right.value
            if operator == "+":
                return Int(line=line, value=a + b)
            if operator == "-":
                return Int(line=line, value=a - b)
            if operator == "*":
                return Int(line=line, value=a * b)
            if operator == "&":
                return Int(line=line, value=a & b)
            if operator == "|":
                return Int(line=line, value=a | b)
            if operator == "^":
                return Int(line=line, value=a ^ b)
            if operator == "/" and b != 0:
                return Int(line=line, value=a // b)
            if operator == "%" and b != 0:
                return Int(line=line, value=a % b)
            if operator == "<<":
                return Int(line=line, value=(a & 65535) << (b & 31) & 65535)
            if operator == ">>":
                return Int(line=line, value=(a & 65535) >> (b & 31))
        # Rewrite `x / 2^N` as `x >> N` — a single shr replaces a ~10-byte
        # div sequence and avoids the slow div instruction.  Only kicks
        # in when N is a positive power of two; other divisions stay as-is.
        if operator == "/" and isinstance(right, Int) and right.value > 0 and (right.value & (right.value - 1)) == 0:
            shift = right.value.bit_length() - 1
            return BinaryOperation(left=left, line=line, operation=">>", right=Int(line=line, value=shift))
        if (
            operator in ("+", "-")
            and isinstance(right, Int)
            and isinstance(left, BinaryOperation)
            and left.operation in ("+", "-")
            and isinstance(left.right, Int)
        ):
            inner_sign = 1 if left.operation == "+" else -1
            outer_sign = 1 if operator == "+" else -1
            combined = inner_sign * left.right.value + outer_sign * right.value
            if combined >= 0:
                return BinaryOperation(left=left.left, line=line, operation="+", right=Int(line=line, value=combined))
            return BinaryOperation(left=left.left, line=line, operation="-", right=Int(line=line, value=-combined))
        return BinaryOperation(left=left, line=line, operation=operator, right=right)

    def peek(self, offset: int = 0) -> tuple[str, str, int]:
        """Return the token at the current position plus an optional offset.

        Returns:
            The token as a (kind, text, line) triple.

        """
        return self.tokens[self.position + offset]

    def _parse_attribute(self, *, line: int) -> tuple[str, object]:
        """Consume a single ``__attribute__((name(args)))`` directive.

        Returns a ``(name, value)`` tuple that the caller dispatches
        on.  Supported kinds:

        * ``("regparm", 1)`` — first arg arrives in AX (fastcall).
        * ``("asm_register", "si")`` — file-scope global aliases SI.
        * ``("carry_return", True)`` — int return is reported via CF
          (CF clear = 1/true/success, CF set = 0/false/failure); no
          parenthesised argument list.
        * ``("always_inline", True)`` — inline the single-asm-body
          function at every C-level call site; no free-standing body.

        clang silently accepts regparm on x86 targets; asm_register /
        carry_return are unknown to clang and produce a
        ``-Wunknown-attributes`` warning (returncode stays 0), so the
        syntax survives ``test_cc.py``.
        """
        self.eat("IDENT")  # __attribute__
        self.eat("LPAREN")
        self.eat("LPAREN")
        attr_name_token = self.eat("IDENT")
        attr_name = attr_name_token[1]
        if attr_name == "regparm":
            self.eat("LPAREN")
            count_token = self.eat("NUMBER")
            self.eat("RPAREN")
            self.eat("RPAREN")
            self.eat("RPAREN")
            count = int(count_token[1])
            if count != 1:
                message = f"regparm({count}) not supported; only regparm(1) is implemented"
                raise CompileError(message, line=line)
            return ("regparm", count)
        if attr_name == "asm_name":
            self.eat("LPAREN")
            sym_token = self.eat("STRING")
            self.eat("RPAREN")
            self.eat("RPAREN")
            self.eat("RPAREN")
            return ("asm_name", sym_token[1][1:-1])
        if attr_name == "asm_register":
            self.eat("LPAREN")
            reg_token = self.eat("STRING")
            self.eat("RPAREN")
            self.eat("RPAREN")
            self.eat("RPAREN")
            reg_name = reg_token[1][1:-1]
            if reg_name != "si":
                message = f"asm_register('{reg_name}') not supported; only 'si' is implemented"
                raise CompileError(message, line=line)
            return ("asm_register", reg_name)
        if attr_name == "carry_return":
            self.eat("RPAREN")
            self.eat("RPAREN")
            return ("carry_return", True)
        if attr_name == "always_inline":
            self.eat("RPAREN")
            self.eat("RPAREN")
            return ("always_inline", True)
        if attr_name == "naked":
            self.eat("RPAREN")
            self.eat("RPAREN")
            return ("naked", True)
        if attr_name == "in_register":
            self.eat("LPAREN")
            reg_token = self.eat("STRING")
            self.eat("RPAREN")
            self.eat("RPAREN")
            self.eat("RPAREN")
            return ("in_register", reg_token[1][1:-1])
        if attr_name == "out_register":
            self.eat("LPAREN")
            reg_token = self.eat("STRING")
            self.eat("RPAREN")
            self.eat("RPAREN")
            self.eat("RPAREN")
            return ("out_register", reg_token[1][1:-1])
        if attr_name == "preserve_register":
            self.eat("LPAREN")
            reg_token = self.eat("STRING")
            self.eat("RPAREN")
            self.eat("RPAREN")
            self.eat("RPAREN")
            return ("preserve_register", reg_token[1][1:-1])
        message = f"unsupported attribute '{attr_name}'"
        raise CompileError(message, line=line)

    def _parse_member_assignment(self) -> MemberAssign:
        """Parse ``name (. | ->) member = expr ;``."""
        token = self.eat("IDENT")
        object_name = token[1]
        arrow_token = self.eat()
        arrow = arrow_token[0] == "ARROW"
        member_token = self.eat("IDENT")
        self.eat("ASSIGN")
        expression = self.parse_expression()
        self.eat("SEMI")
        return MemberAssign(
            arrow=arrow,
            expr=expression,
            line=token[2],
            member_name=member_token[1],
            object_name=object_name,
        )

    def _parse_struct_declaration(self) -> StructDecl:
        """Parse ``struct NAME { type field; ... };`` at file scope."""
        line = self.peek()[2]
        self.eat("STRUCT")
        name_token = self.eat("IDENT")
        name = name_token[1]
        self.eat("LBRACE")
        fields: list[StructField] = []
        while self.peek()[0] != "RBRACE":
            field_type = self.parse_type()
            if self.peek()[0] == "LPAREN":
                # Function pointer field: type (*field_name)(params)
                self.eat("LPAREN")
                self.eat("STAR")
                field_name = self.eat("IDENT")[1]
                self.eat("RPAREN")
                self.eat("LPAREN")
                self.parse_parameters()  # consume param list; size is always 2
                self.eat("RPAREN")
                field_type = "function_pointer"
            else:
                field_name = self.eat("IDENT")[1]
            # Optional [N] for fixed-size array fields (e.g. ``char _reserved[15]``).
            if self.peek()[0] == "LBRACKET":
                self.eat("LBRACKET")
                count_token = self.eat("NUMBER")
                self.eat("RBRACKET")
                field_type = f"{field_type}[{count_token[1]}]"
            self.eat("SEMI")
            fields.append(StructField(field_name=field_name, line=line, type_name=field_type))
        self.eat("RBRACE")
        self.eat("SEMI")
        decl = StructDecl(fields=fields, line=line, name=name)
        self.struct_decls[name] = decl
        return decl

    def _parse_struct_init(self) -> Node:
        """Parse a brace-enclosed struct element initializer ``{a, b, ...}``.

        Fields are positional.  Trailing commas are accepted.

        Returns:
            A ``StructInit`` node.

        """
        line = self.peek()[2]
        self.eat("LBRACE")
        fields = []
        while self.peek()[0] != "RBRACE":
            fields.append(self.parse_expression())
            if self.peek()[0] == "COMMA":
                self.eat("COMMA")
            else:
                break
        self.eat("RBRACE")
        return StructInit(fields=fields, line=line)

    def _parse_tail_call(self) -> Node:
        """Parse a ``__tail_call(fn_ptr, arg1, ...)`` statement.

        The first token is the ``__tail_call`` identifier; the
        remaining syntax is ``(fn_ptr_name, arg_expr, ...) ;``.
        ``fn_ptr_name`` must name a local ``function_pointer`` variable;
        the arguments map to its ``in_register`` parameters in order.
        """
        token = self.eat("IDENT")  # __tail_call
        self.eat("LPAREN")
        fn_token = self.eat("IDENT")
        fn = fn_token[1]
        args = []
        while self.peek()[0] == "COMMA":
            self.eat("COMMA")
            args.append(self.parse_expression())
        self.eat("RPAREN")
        self.eat("SEMI")
        return TailCall(args=args, fn=fn, line=token[2])

    def parse_additive(self) -> Node:
        """Parse an additive expression (addition and subtraction).

        Returns:
            An AST node for the additive expression.

        """
        node = self.parse_multiplicative()
        while self.peek()[0] in ADDITIVE_OPERATORS:
            operator_token = self.eat()
            right = self.parse_multiplicative()
            node = self.fold_binop(operator_token[1], node, right)
        return node

    def parse_arguments(self) -> list[Node]:
        """Parse a comma-separated argument list through the closing paren.

        Returns:
            A list of AST expression nodes.

        """
        arguments: list[Node] = []
        if self.peek()[0] != "RPAREN":
            arguments.append(self.parse_expression())
            while self.peek()[0] == "COMMA":
                self.eat("COMMA")
                arguments.append(self.parse_expression())
        self.eat("RPAREN")
        return arguments

    def parse_array_init(self) -> Node:
        """Parse a brace-enclosed array initializer.

        Each element may itself be a brace-enclosed struct initializer
        ``{a, b, ...}``, in which case it is returned as a ``StructInit``
        node.  Trailing commas are accepted.

        Returns:
            An AST node for the array initializer.

        """
        line = self.peek()[2]
        self.eat("LBRACE")
        elems = []
        while self.peek()[0] != "RBRACE":
            if self.peek()[0] == "LBRACE":
                elems.append(self._parse_struct_init())
            else:
                elems.append(self.parse_expression())
            if self.peek()[0] == "COMMA":
                self.eat("COMMA")
            else:
                break
        self.eat("RBRACE")
        return ArrayInit(elements=elems, line=line)

    def parse_assignment(self) -> Node:
        """Parse a simple assignment statement.

        Returns:
            An AST node for the assignment.

        """
        token = self.eat("IDENT")
        name = token[1]
        self.eat("ASSIGN")
        expression = self.parse_expression()
        self.eat("SEMI")
        return Assign(expr=expression, line=token[2], name=name)

    def parse_bitwise_and(self) -> Node:
        """Parse a left-associative bitwise ``&`` expression.

        Returns:
            A ``BinaryOperation`` chain or the underlying comparison.

        """
        left = self.parse_comparison()
        while self.peek()[0] == "AMP":
            self.eat()
            right = self.parse_comparison()
            left = self.fold_binop("&", left, right)
        return left

    def parse_bitwise_or(self) -> Node:
        """Parse a left-associative bitwise ``|`` expression.

        Lower precedence than ``^`` and ``&``, higher than ``&&``.
        """
        left = self.parse_bitwise_xor()
        while self.peek()[0] == "PIPE":
            self.eat()
            right = self.parse_bitwise_xor()
            left = self.fold_binop("|", left, right)
        return left

    def parse_bitwise_xor(self) -> Node:
        """Parse a left-associative bitwise ``^`` expression.

        Lower precedence than ``&``, higher than ``|``.
        """
        left = self.parse_bitwise_and()
        while self.peek()[0] == "CARET":
            self.eat()
            right = self.parse_bitwise_and()
            left = self.fold_binop("^", left, right)
        return left

    def parse_block(self) -> list[Node]:
        """Parse statements until a closing brace and consume it.

        Returns:
            A list of AST statement nodes.

        """
        body: list[Node] = []
        while self.peek()[0] != "RBRACE":
            body.append(self.parse_statement())
        self.eat("RBRACE")
        return body

    def parse_call_statement(self) -> Node:
        """Parse a function call statement.

        Returns:
            An AST node for the call statement.

        """
        token = self.eat("IDENT")
        name = token[1]
        self.eat("LPAREN")
        arguments = self.parse_arguments()
        self.eat("SEMI")
        return Call(args=arguments, line=token[2], name=name)

    def parse_comparison(self) -> Node:
        """Parse a comparison expression.

        Returns:
            An AST node for the comparison expression.

        """
        left = self.parse_shift()
        if self.peek()[0] in COMPARISON_OPERATORS:
            operator_token = self.eat()
            right = self.parse_shift()
            return BinaryOperation(left=left, line=operator_token[2], operation=operator_token[1], right=right)
        return left

    def parse_compound_assignment(self) -> Node:
        """Parse a compound assignment (``+=``, ``&=``, ``|=``, ``^=``, ``<<=``, ``>>=``).

        Returns:
            An AST node for the desugared assignment ``x = x operation rhs``.

        """
        token = self.eat("IDENT")
        name = token[1]
        line = token[2]
        operator_token = self.eat()
        operator = COMPOUND_ASSIGN_OPERATORS[operator_token[0]]
        expression = self.parse_expression()
        self.eat("SEMI")
        return Assign(
            expr=BinaryOperation(left=Var(line=line, name=name), line=line, operation=operator, right=expression), line=line, name=name
        )

    def parse_condition(self) -> Node:
        """Parse an if/while condition.

        Wraps a bare expression as ``expr != 0`` so that ``if (error)``
        is equivalent to ``if (error != 0)``.  Comparisons at the top
        level are returned unchanged.

        Returns:
            A BinaryOperation AST node suitable for conditional jumps.

        """
        expression = self.parse_expression()
        if isinstance(expression, (LogicalAnd, LogicalOr)):
            return expression
        if isinstance(expression, BinaryOperation) and expression.operation in COMPARISON_OPERATIONS:
            return expression
        return BinaryOperation(left=expression, line=expression.line, operation="!=", right=Int(line=expression.line, value=0))

    def parse_do_while(self) -> Node:
        """Parse a do...while loop statement.

        Returns:
            A ``DoWhile`` AST node.

        """
        token = self.eat("DO")
        self.eat("LBRACE")
        body = self.parse_block()
        self.eat("WHILE")
        self.eat("LPAREN")
        condition = self.parse_condition()
        self.eat("RPAREN")
        self.eat("SEMI")
        return DoWhile(body=body, cond=condition, line=token[2])

    def parse_expression(self) -> Node:
        """Parse an expression.

        Returns:
            An AST node for the expression.

        """
        return self.parse_logical_or()

    def parse_if(self) -> Node:
        """Parse an if statement.

        Returns:
            An AST node for the if statement.

        """
        token = self.eat("IF")
        self.eat("LPAREN")
        condition = self.parse_condition()
        self.eat("RPAREN")
        self.eat("LBRACE")
        body = self.parse_block()
        else_body: list[Node] | None = None
        if self.peek()[0] == "ELSE":
            self.eat("ELSE")
            if self.peek()[0] == "IF":
                else_body = [self.parse_if()]
            else:
                self.eat("LBRACE")
                else_body = self.parse_block()
        return If(body=body, cond=condition, else_body=else_body, line=token[2])

    def parse_index_assignment(self) -> Node:
        """Parse an indexed assignment ``name[index] = expr;``."""
        token = self.eat("IDENT")
        name = token[1]
        self.eat("LBRACKET")
        index = self.parse_expression()
        self.eat("RBRACKET")
        self.eat("ASSIGN")
        expr = self.parse_expression()
        self.eat("SEMI")
        return IndexAssign(expr=expr, index=index, line=token[2], name=name)

    def parse_logical_and(self) -> Node:
        """Parse a left-associative ``&&`` expression.

        Returns:
            A ``LogicalAnd`` tree or the underlying bitwise-OR node.

        """
        left = self.parse_bitwise_or()
        while self.peek()[0] == "AND_AND":
            operator_token = self.eat()
            right = self.parse_bitwise_or()
            left = LogicalAnd(left=left, line=operator_token[2], right=right)
        return left

    def parse_logical_or(self) -> Node:
        """Parse a left-associative ``||`` expression.

        Returns:
            A ``LogicalOr`` tree or the underlying ``&&`` node.

        """
        left = self.parse_logical_and()
        while self.peek()[0] == "OR_OR":
            operator_token = self.eat()
            right = self.parse_logical_and()
            left = LogicalOr(left=left, line=operator_token[2], right=right)
        return left

    def parse_multiplicative(self) -> Node:
        """Parse a multiplicative expression (multiplication and division).

        Returns:
            An AST node for the multiplicative expression.

        """
        node = self.parse_primary()
        while self.peek()[0] in MULTIPLICATIVE_OPERATORS:
            operator_token = self.eat()
            right = self.parse_primary()
            node = self.fold_binop(operator_token[1], node, right)
        return node

    def parse_parameter(self) -> Param:
        """Parse a single function parameter.

        Returns:
            A Param dataclass.

        """
        type_string = self.parse_type()
        name_token = self.eat("IDENT")
        name = name_token[1]
        in_register: str | None = None
        is_array = False
        out_register: str | None = None
        if self.peek()[0] == "LBRACKET":
            self.eat("LBRACKET")
            self.eat("RBRACKET")
            is_array = True
        if self.peek()[0] == "IDENT" and self.peek()[1] == "__attribute__":
            kind, value = self._parse_attribute(line=name_token[2])
            if kind == "in_register":
                in_register = value
            elif kind == "out_register":
                out_register = value
            else:
                message = f"unsupported parameter attribute '{kind}'"
                raise CompileError(message, line=name_token[2])
        return Param(in_register=in_register, is_array=is_array, name=name, out_register=out_register, type=type_string)

    def parse_parameters(self) -> list[Param]:
        """Parse a function parameter list.

        Returns:
            A list of Param dataclasses.

        """
        if self.peek()[0] == "RPAREN":
            return []
        parameters = [self.parse_parameter()]
        while self.peek()[0] == "COMMA":
            self.eat("COMMA")
            parameters.append(self.parse_parameter())
        return parameters

    def parse_primary(self) -> Node:
        """Parse a primary expression (literals, variables, indexing, parens).

        Returns:
            An AST node for the primary expression.

        Raises:
            CompileError: If an unexpected token is encountered.

        """
        token = self.peek()
        line = token[2]
        if token[0] == "SIZEOF":
            return self.parse_sizeof()
        if token[0] == "NUMBER":
            self.eat()
            return Int(line=line, value=int(token[1], 0))
        if token[0] == "CHAR_LIT":
            self.eat()
            return Char(line=line, value=decode_first_character(token[1][1:-1], line=line))
        if token[0] == "STRING":
            self.eat()
            content = token[1][1:-1]
            # Adjacent string literals concatenate — standard C behavior.
            # ``"foo" "bar"`` folds to ``"foobar"`` at parse time.
            while self.peek()[0] == "STRING":
                content += self.eat()[1][1:-1]
            return String(content=content, line=line)
        if token[0] == "IDENT":
            self.eat()
            if self.peek()[0] == "LPAREN":
                self.eat("LPAREN")
                return Call(args=self.parse_arguments(), line=line, name=token[1])
            if self.peek()[0] == "LBRACKET":
                self.eat("LBRACKET")
                index = self.parse_expression()
                self.eat("RBRACKET")
                return Index(index=index, line=line, name=token[1])
            if self.peek()[0] in ("DOT", "ARROW"):
                arrow_token = self.eat()
                arrow = arrow_token[0] == "ARROW"
                member_token = self.eat("IDENT")
                # ``ptr->field[i]`` indexes into an array-typed member.
                if self.peek()[0] == "LBRACKET":
                    self.eat("LBRACKET")
                    index = self.parse_expression()
                    self.eat("RBRACKET")
                    return MemberIndex(
                        arrow=arrow,
                        index=index,
                        line=line,
                        member_name=member_token[1],
                        object_name=token[1],
                    )
                return MemberAccess(
                    arrow=arrow,
                    line=line,
                    member_name=member_token[1],
                    object_name=token[1],
                )
            return Var(line=line, name=token[1])
        if token[0] == "NOT":
            self.eat()
            return BinaryOperation(left=self.parse_primary(), line=line, operation="==", right=Int(line=line, value=0))
        if token[0] == "TILDE":
            self.eat()
            operand = self.parse_primary()
            if isinstance(operand, Int):
                return Int(line=line, value=operand.value ^ 65535)
            return BinaryOperation(left=operand, line=line, operation="^", right=Int(line=line, value=65535))
        if token[0] == "MINUS":
            self.eat()
            operand = self.parse_primary()
            # Fold ``-<int>`` to a single negative ``Int`` so ``-1`` and ``-42``
            # round-trip as literals instead of an addition node.  Runtime
            # negation still rewrites to ``0 - x`` to reuse the subtract path.
            if isinstance(operand, Int):
                return Int(line=line, value=-operand.value)
            return BinaryOperation(left=Int(line=line, value=0), line=line, operation="-", right=operand)
        if token[0] == "AMP":
            self.eat()
            name_token = self.eat("IDENT")
            # ``&array[i]`` desugars to ``array + i`` — cc.py's typed pointer
            # arithmetic already scales by element size, so the resulting
            # BinaryOperation produces the same address.
            if self.peek()[0] == "LBRACKET":
                self.eat("LBRACKET")
                index = self.parse_expression()
                self.eat("RBRACKET")
                return BinaryOperation(
                    left=Var(line=line, name=name_token[1]),
                    line=line,
                    operation="+",
                    right=index,
                )
            return AddressOf(line=line, name=name_token[1])
        if token[0] == "LPAREN":
            self.eat()
            expression = self.parse_expression()
            self.eat("RPAREN")
            return expression
        message = f"expected expression, got {token[0]} ({token[1]!r})"
        raise CompileError(message, line=line)

    def parse_program(self) -> Node:
        """Parse the entire program as a sequence of top-level declarations.

        Top-level declarations are either function definitions or
        file-scope (``global``) variable / array declarations.  Both
        start with ``type IDENT``; the token after the name
        disambiguates — ``(`` introduces a function, anything else a
        variable.
        """
        line = self.peek()[2]
        functions: list[Node] = []
        globals_list: list[Node] = []
        while self.peek()[0] != "EOF":
            declaration = self.parse_top_level_declaration()
            if isinstance(declaration, Function):
                functions.append(declaration)
            elif declaration is not None:
                globals_list.append(declaration)
        return Program(functions=functions, globals=globals_list, line=line)

    def parse_shift(self) -> Node:
        """Parse a shift expression (``<<`` and ``>>``).

        Higher precedence than comparison, lower than additive — matches
        C's precedence order.
        """
        node = self.parse_additive()
        while self.peek()[0] in SHIFT_OPERATORS:
            operator_token = self.eat()
            right = self.parse_additive()
            node = self.fold_binop(operator_token[1], node, right)
        return node

    def parse_sizeof(self) -> Node:
        """Parse a sizeof expression.

        Returns:
            An AST node for sizeof(type) or sizeof(variable).

        """
        token = self.eat("SIZEOF")
        self.eat("LPAREN")
        # sizeof(type) or sizeof(variable)
        if self.peek()[0] in TYPE_TOKENS:
            type_string = self.parse_type()
            self.eat("RPAREN")
            return SizeofType(line=token[2], type_name=type_string)
        name = self.eat("IDENT")[1]
        self.eat("RPAREN")
        return SizeofVar(line=token[2], name=name)

    def parse_statement(self) -> Node:
        """Parse a single statement.

        Returns:
            An AST node for the statement.

        Raises:
            CompileError: If an unexpected token is encountered.

        """
        token = self.peek()
        if token[0] in TYPE_TOKENS:
            return self.parse_variable_declaration()
        if token[0] == "IF":
            return self.parse_if()
        if token[0] == "BREAK":
            self.eat("BREAK")
            self.eat("SEMI")
            return Break(line=token[2])
        if token[0] == "CONTINUE":
            self.eat("CONTINUE")
            self.eat("SEMI")
            return Continue(line=token[2])
        if token[0] == "DO":
            return self.parse_do_while()
        if token[0] == "RETURN":
            self.eat("RETURN")
            value = None
            if self.peek()[0] != "SEMI":
                value = self.parse_expression()
            self.eat("SEMI")
            return Return(line=token[2], value=value)
        if token[0] == "WHILE":
            return self.parse_while()
        if token[0] == "STAR":
            self.eat("STAR")
            name_token = self.eat("IDENT")
            self.eat("ASSIGN")
            expr = self.parse_expression()
            self.eat("SEMI")
            return DerefAssign(expr=expr, line=token[2], name=name_token[1])
        if token[0] == "IDENT":
            next_kind = self.peek(offset=1)[0]
            if next_kind == "ASSIGN":
                return self.parse_assignment()
            if next_kind in COMPOUND_ASSIGN_OPERATORS:
                return self.parse_compound_assignment()
            if next_kind == "LBRACKET":
                return self.parse_index_assignment()
            if next_kind in ("DOT", "ARROW"):
                return self._parse_member_assignment()
            if token[1] == "__tail_call":
                return self._parse_tail_call()
            return self.parse_call_statement()
        message = f"expected statement, got {token[0]} ({token[1]!r})"
        raise CompileError(message, line=token[2])

    def parse_top_level_declaration(self) -> Node:
        """Parse a function definition, a file-scope variable / array, or a file-scope ``asm(...)``.

        Dispatches on the token after ``type IDENT``: ``(`` drives the
        function path, any other token means a global declaration.  A
        bare ``asm("...");`` at the top level is emitted verbatim into
        the output's data tail — useful for raw tables and labels.
        """
        line = self.peek()[2]
        if self.peek()[0] == "STRUCT" and self.peek(offset=1)[0] == "IDENT" and self.peek(offset=2)[0] == "LBRACE":
            return self._parse_struct_declaration()
        if self.peek()[0] == "IDENT" and self.peek()[1] == "asm" and self.peek(offset=1)[0] == "LPAREN":
            self.eat("IDENT")
            self.eat("LPAREN")
            string_token = self.eat("STRING")
            content = string_token[1][1:-1]
            # Adjacent string literals concatenate (as in parse_primary).
            while self.peek()[0] == "STRING":
                content += self.eat()[1][1:-1]
            self.eat("RPAREN")
            self.eat("SEMI")
            return InlineAsm(content=content, line=line)
        # Optional leading ``__attribute__((...))`` directives.
        # ``regparm(1)`` applies to function definitions (arg 0 in AX);
        # ``asm_register("REG")`` applies to file-scope VarDecls (the
        # variable aliases the named CPU register).  Both may appear
        # before the return type.  ``regparm`` may also appear after
        # the function parameter list; ``asm_register`` is leading-only.
        regparm_count = 0
        asm_register: str | None = None
        asm_symbol: str | None = None
        carry_return = False
        always_inline = False
        naked = False
        preserve_registers: list[str] = []
        while self.peek()[0] == "IDENT" and self.peek()[1] == "__attribute__":
            kind, value = self._parse_attribute(line=line)
            if kind == "regparm":
                regparm_count = value
            elif kind == "carry_return":
                carry_return = True
            elif kind == "always_inline":
                always_inline = True
            elif kind == "naked":
                naked = True
            elif kind == "preserve_register":
                preserve_registers.append(value)
            elif kind == "asm_name":
                asm_symbol = value
            else:
                asm_register = value
        type_string = self.parse_type()
        name_token = self.eat("IDENT")
        name = name_token[1]
        if self.peek()[0] == "LPAREN":
            if asm_register is not None:
                message = "asm_register attribute is not valid on function definitions"
                raise CompileError(message, line=line)
            self.eat("LPAREN")
            parameters = self.parse_parameters()
            self.eat("RPAREN")
            while self.peek()[0] == "IDENT" and self.peek()[1] == "__attribute__":
                kind, value = self._parse_attribute(line=line)
                if kind == "regparm":
                    if regparm_count != 0:
                        message = "regparm attribute specified twice"
                        raise CompileError(message, line=line)
                    regparm_count = value
                elif kind == "carry_return":
                    carry_return = True
                elif kind == "always_inline":
                    always_inline = True
                elif kind == "naked":
                    naked = True
                elif kind == "preserve_register":
                    preserve_registers.append(value)
                else:
                    message = f"trailing {kind} attribute is not valid on function definitions"
                    raise CompileError(message, line=line)
            if regparm_count > 0 and not parameters:
                message = "regparm(1) requires at least one parameter"
                raise CompileError(message, line=line)
            stack_param_count = sum(1 for p in parameters if p.out_register is None and p.in_register is None)
            if carry_return and stack_param_count > regparm_count:
                # Stack-passed args would require an ``add sp, N`` cleanup
                # after the call, which clobbers CF.  carry_return callees
                # must arrive via AX only (regparm(1)), take no args, or
                # use only out_register/in_register params (no stack push, no cleanup).
                message = "carry_return functions may not take stack args; use 0 params, out_register/in_register params, or regparm(1)"
                raise CompileError(message, line=line)
            if always_inline and stack_param_count > regparm_count:
                # Inlining splices the body in place; stack args would
                # need a caller-side cleanup that doesn't exist.
                message = "always_inline functions may not take stack args; use 0 params, out_register/in_register params, or regparm(1)"
                raise CompileError(message, line=line)
            if self.peek()[0] == "SEMI":
                # Function prototype (no body).  Retained in the AST so
                # the generator can register calling-convention metadata
                # (carry_return, out_register params) for external
                # functions called from C.  No code is emitted for
                # prototype nodes.
                self.eat("SEMI")
                return Function(
                    always_inline=always_inline,
                    body=[],
                    carry_return=carry_return,
                    is_prototype=True,
                    line=line,
                    naked=naked,
                    name=name,
                    params=parameters,
                    preserve_registers=preserve_registers,
                    regparm_count=regparm_count,
                )
            self.eat("LBRACE")
            return Function(
                always_inline=always_inline,
                body=self.parse_block(),
                carry_return=carry_return,
                line=line,
                naked=naked,
                name=name,
                params=parameters,
                preserve_registers=preserve_registers,
                regparm_count=regparm_count,
            )
        if regparm_count != 0:
            message = "regparm attribute is not valid on global variables"
            raise CompileError(message, line=line)
        if carry_return:
            message = "carry_return attribute is not valid on global variables"
            raise CompileError(message, line=line)
        if always_inline:
            message = "always_inline attribute is not valid on global variables"
            raise CompileError(message, line=line)
        if naked:
            message = "naked attribute is not valid on global variables"
            raise CompileError(message, line=line)
        if preserve_registers:
            message = "preserve_register attribute is not valid on global variables"
            raise CompileError(message, line=line)
        # Trailing ``__attribute__`` on the variable name (e.g. ``uint16_t x __attribute__((asm_name("sym")))``)
        # is equivalent to a leading one for global variable declarations.
        while self.peek()[0] == "IDENT" and self.peek()[1] == "__attribute__":
            kind, value = self._parse_attribute(line=line)
            if kind == "asm_name":
                asm_symbol = value
            elif kind == "asm_register":
                asm_register = value
            else:
                message = f"trailing {kind} attribute is not valid on global variable declarations"
                raise CompileError(message, line=line)
        # File-scope variable: scalar or array.  Globals may specify a
        # size inside ``[...]`` (unlike locals) since there is no
        # runtime initializer to imply one.
        is_array = False
        size_expression: Node | None = None
        if self.peek()[0] == "LBRACKET":
            self.eat("LBRACKET")
            is_array = True
            if self.peek()[0] != "RBRACKET":
                size_expression = self.parse_expression()
            self.eat("RBRACKET")
        init: Node | None = None
        if self.peek()[0] == "ASSIGN":
            self.eat("ASSIGN")
            init = self.parse_array_init() if is_array else self.parse_expression()
        self.eat("SEMI")
        if is_array:
            if asm_register is not None:
                message = "asm_register attribute is not valid on arrays"
                raise CompileError(message, line=line)
            if asm_symbol is not None:
                message = "asm_name attribute is not valid on arrays"
                raise CompileError(message, line=line)
            if size_expression is None and init is None:
                message = f"global array '{name}' needs either a size or an initializer"
                raise CompileError(message, line=line)
            return ArrayDecl(init=init, line=line, name=name, size=size_expression, type_name=type_string)
        return VarDecl(asm_register=asm_register, asm_symbol=asm_symbol, init=init, line=line, name=name, type_name=type_string)

    def parse_type(self) -> str:
        """Parse a type specifier (void, int, char, char*, uint8_t, uint8_t*, uint16_t, uint16_t*, uint32_t, uint32_t*, unsigned long).

        An optional leading ``const`` is accepted and discarded — the C
        subset has no notion of const-ness but tolerating the keyword
        lets sources carry POSIX-compatible signatures (e.g. ``int
        strcmp(const char *, const char *)``) that ``<string.h>``
        expects when the same source is syntax-checked by clang.

        Returns:
            The type as a string.

        Raises:
            CompileError: If an unexpected token is encountered, or a bare
                ``long`` / ``unsigned`` without ``long`` appears.

        """
        if self.peek()[0] == "CONST":
            self.eat()
        token = self.peek()
        if token[0] == "VOID":
            self.eat()
            return "void"
        if token[0] == "INT":
            self.eat()
            if self.peek()[0] == "STAR":
                self.eat()
                return "int*"
            return "int"
        if token[0] == "CHAR":
            self.eat()
            if self.peek()[0] == "STAR":
                self.eat()
                return "char*"
            return "char"
        if token[0] == "UINT8_T":
            self.eat()
            if self.peek()[0] == "STAR":
                self.eat()
                return "uint8_t*"
            return "uint8_t"
        if token[0] == "UINT16_T":
            self.eat()
            if self.peek()[0] == "STAR":
                self.eat()
                return "uint16_t*"
            return "uint16_t"
        if token[0] == "UINT32_T":
            self.eat()
            if self.peek()[0] == "STAR":
                self.eat()
                return "uint32_t*"
            return "uint32_t"
        if token[0] == "UNSIGNED":
            self.eat()
            if self.peek()[0] != "LONG":
                following = self.peek()
                message = f"expected 'long' after 'unsigned', got {following[1]!r}"
                raise CompileError(message, line=token[2])
            self.eat()
            return "unsigned long"
        if token[0] == "LONG":
            message = "bare 'long' is not supported; use 'unsigned long'"
            raise CompileError(message, line=token[2])
        if token[0] == "STRUCT":
            self.eat()
            tag_token = self.eat("IDENT")
            tag = tag_token[1]
            if self.peek()[0] == "STAR":
                self.eat()
                return f"struct {tag}*"
            return f"struct {tag}"
        message = f"expected type, got {token[0]} ({token[1]!r})"
        raise CompileError(message, line=token[2])

    def parse_variable_declaration(self) -> Node:
        """Parse a variable or array declaration.

        Returns:
            An AST node for the declaration.

        """
        line = self.peek()[2]
        type_string = self.parse_type()
        function_pointer_params_list: list[Param] | None = None
        if self.peek()[0] == "LPAREN":
            # Function pointer variable: type (*name)(params)
            self.eat("LPAREN")
            self.eat("STAR")
            name = self.eat("IDENT")[1]
            self.eat("RPAREN")
            self.eat("LPAREN")
            function_pointer_params_list = self.parse_parameters()
            self.eat("RPAREN")
            type_string = "function_pointer"
        else:
            name = self.eat("IDENT")[1]
        # Optional [] or [N] for array declarations
        is_array = False
        size_expression: Node | None = None
        if self.peek()[0] == "LBRACKET":
            self.eat("LBRACKET")
            is_array = True
            if self.peek()[0] != "RBRACKET":
                size_expression = self.parse_expression()
            self.eat("RBRACKET")
        init = None
        if self.peek()[0] == "ASSIGN":
            self.eat("ASSIGN")
            init = self.parse_array_init() if is_array else self.parse_expression()
        self.eat("SEMI")
        if is_array:
            return ArrayDecl(init=init, line=line, name=name, size=size_expression, type_name=type_string)
        return VarDecl(function_pointer_params=function_pointer_params_list, init=init, line=line, name=name, type_name=type_string)

    def parse_while(self) -> Node:
        """Parse a while loop statement.

        Returns:
            An AST node for the while loop.

        """
        token = self.eat("WHILE")
        self.eat("LPAREN")
        condition = self.parse_condition()
        self.eat("RPAREN")
        self.eat("LBRACE")
        return While(body=self.parse_block(), cond=condition, line=token[2])
