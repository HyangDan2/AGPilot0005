"""Safe Math Calculator Tool (No eval, AST parsed)."""
import ast
import math
import operator
from typing import Any, Dict

SAFE_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

SAFE_FUNCTIONS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "ceil": math.ceil,
    "floor": math.floor,
    "log": math.log,
    "log10": math.log10,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "pi": math.pi,
    "e": math.e,
}

class SafeEvalVisitor(ast.NodeVisitor):
    def visit(self, node: ast.AST) -> Any:
        method = "visit_" + node.__class__.__name__
        visitor = getattr(self, method, self.generic_visit)
        return visitor(node)

    def generic_visit(self, node: ast.AST) -> Any:
        raise ValueError(f"연산에 허용되지 않는 구문입니다: {node.__class__.__name__}")

    def visit_Expression(self, node: ast.Expression) -> Any:
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> Any:
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("숫자 상수만 지원됩니다.")

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        op_type = type(node.op)
        if op_type in SAFE_OPERATORS:
            val = self.visit(node.operand)
            return SAFE_OPERATORS[op_type](val)
        raise ValueError("지원되지 않는 단항 연산자입니다.")

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        op_type = type(node.op)
        if op_type in SAFE_OPERATORS:
            left = self.visit(node.left)
            right = self.visit(node.right)
            if op_type == ast.Pow and (right > 1000 or (isinstance(left, (int, float)) and left > 1000 and right > 10)):
                raise ValueError("연산 결과가 너무 커서 계산할 수 없습니다.")
            return SAFE_OPERATORS[op_type](left, right)
        raise ValueError("지원되지 않는 이항 연산자입니다.")

    def visit_Call(self, node: ast.Call) -> Any:
        if not isinstance(node.func, ast.Name):
            raise ValueError("호출 가능한 함수가 아닙니다.")
        func_name = node.func.id
        if func_name not in SAFE_FUNCTIONS:
            raise ValueError(f"허용되지 않는 수학 함수입니다: {func_name}")
        func = SAFE_FUNCTIONS[func_name]
        if callable(func):
            args = [self.visit(arg) for arg in node.args]
            return func(*args)
        return func

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id in SAFE_FUNCTIONS and not callable(SAFE_FUNCTIONS[node.id]):
            return SAFE_FUNCTIONS[node.id]
        raise ValueError(f"알 수 없는 변수 또는 식별자입니다: {node.id}")

async def calculate(expression: str) -> Dict[str, Any]:
    """Calculates mathematical expression using safe AST evaluation (No eval)."""
    if len(expression) > 300:
        raise ValueError("수식이 너무 깁니다. 300자 이하로 입력해 주세요.")
    try:
        parsed = ast.parse(expression.strip(), mode="eval")
        visitor = SafeEvalVisitor()
        res = visitor.visit(parsed)
        return {
            "expression": expression,
            "result": res,
            "formatted": f"{res:,}" if isinstance(res, (int, float)) and not math.isnan(res) and not math.isinf(res) else str(res),
        }
    except Exception as e:
        return {
            "expression": expression,
            "error": True,
            "message": str(e),
        }
