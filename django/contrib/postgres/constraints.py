from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from django.db.backends.ddl_references import Statement, Table
from django.db.models import Deferrable, F, Func, Q
from django.db.models.constraints import BaseConstraint, Deferrable
from django.db.models.expressions import BaseExpression
from django.db.models.sql import Query

__all__ = ['ExclusionConstraint', 'ConstraintTrigger', 'TriggerEvent']


class TriggerEvent(Enum):
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


_TriggerEventLike = Union[Literal["INSERT", "UPDATE", "DELETE"], TriggerEvent]


class ConstraintTrigger(BaseConstraint):
    template = """
    CREATE CONSTRAINT TRIGGER %(name)s
    AFTER %(events)s ON %(table)s %(deferrable)s
    FOR EACH ROW %(condition)s
    EXECUTE PROCEDURE %(procedure)s
    """.strip()
    delete_template = "DROP TRIGGER %(name)s ON %(table)s"

    def __init__(
        self,
        *,
        name: str,
        events: Union[List[_TriggerEventLike], Tuple[_TriggerEventLike, ...]],
        function: Func,
        condition: Optional[BaseExpression] = None,
        deferrable: Optional[Deferrable] = None,
    ):
        if not events:
            raise ValueError(
                "ConstraintTrigger events must be a list of at least one TriggerEvent"
            )
        self.events = tuple(
            e.value if isinstance(e, TriggerEvent) else str(e).upper() for e in events
        )
        self.function = function
        self.condition = condition
        self.deferrable = deferrable
        super().__init__(name)

    def __eq__(self, other):
        if isinstance(other, self.__class__):
            return (
                self.name == other.name
                and set(self.events) == set(other.events)
                and self.function == other.function
                and self.condition == other.condition
                and self.deferrable == other.deferrable
            )
        return super().__eq__(other)

    def _get_condition_sql(self, compiler, schema_editor, query) -> str:
        if self.condition is None:
            return ""
        sql, params = self.condition.as_sql(compiler, schema_editor.connection)
        condition_sql = sql % tuple(schema_editor.quote_value(p) for p in params)
        return "WHEN %s" % (condition_sql)

    def _get_procedure_sql(self, compiler, schema_editor) -> str:
        sql, params = self.function.as_sql(compiler, schema_editor.connection)
        return sql % tuple(schema_editor.quote_value(p) for p in params)

    def create_sql(self, model, schema_editor) -> Statement:
        table = Table(model._meta.db_table, schema_editor.quote_name)
        query = Query(model, alias_cols=False)
        compiler = query.get_compiler(connection=schema_editor.connection)
        condition = self._get_condition_sql(compiler, schema_editor, query)
        return Statement(
            self.template,
            name=schema_editor.quote_name(self.name),
            events=" OR ".join(self.events),
            table=table,
            condition=condition,
            deferrable=schema_editor._deferrable_constraint_sql(self.deferrable),
            procedure=self._get_procedure_sql(compiler, schema_editor),
        )

    def remove_sql(self, model, schema_editor) -> Statement:
        return Statement(
            self.delete_template,
            table=Table(model._meta.db_table, schema_editor.quote_name),
            name=schema_editor.quote_name(self.name),
        )

    def deconstruct(self) -> Tuple[str, Tuple[Any, ...], Dict[str, Any]]:
        path = "%s.%s" % (self.__class__.__module__, self.__class__.__name__)
        kwargs = {
            "name": self.name,
            "events": self.events,
            "function": self.function,
        }
        if self.condition:
            kwargs["condition"] = self.condition
        if self.deferrable is not None:
            kwargs["deferrable"] = self.deferrable
        return path, (), kwargs


class ExclusionConstraint(BaseConstraint):
    template = 'CONSTRAINT %(name)s EXCLUDE USING %(index_type)s (%(expressions)s)%(where)s%(deferrable)s'

    def __init__(
        self, *, name, expressions, index_type=None, condition=None,
        deferrable=None,
    ):
        if index_type and index_type.lower() not in {'gist', 'spgist'}:
            raise ValueError(
                'Exclusion constraints only support GiST or SP-GiST indexes.'
            )
        if not expressions:
            raise ValueError(
                'At least one expression is required to define an exclusion '
                'constraint.'
            )
        if not all(
            isinstance(expr, (list, tuple)) and len(expr) == 2
            for expr in expressions
        ):
            raise ValueError('The expressions must be a list of 2-tuples.')
        if not isinstance(condition, (type(None), Q)):
            raise ValueError(
                'ExclusionConstraint.condition must be a Q instance.'
            )
        if condition and deferrable:
            raise ValueError(
                'ExclusionConstraint with conditions cannot be deferred.'
            )
        if not isinstance(deferrable, (type(None), Deferrable)):
            raise ValueError(
                'ExclusionConstraint.deferrable must be a Deferrable instance.'
            )
        self.expressions = expressions
        self.index_type = index_type or 'GIST'
        self.condition = condition
        self.deferrable = deferrable
        super().__init__(name=name)

    def _get_expression_sql(self, compiler, connection, query):
        expressions = []
        for expression, operator in self.expressions:
            if isinstance(expression, str):
                expression = F(expression)
            expression = expression.resolve_expression(query=query)
            sql, params = expression.as_sql(compiler, connection)
            expressions.append('%s WITH %s' % (sql % params, operator))
        return expressions

    def _get_condition_sql(self, compiler, schema_editor, query):
        if self.condition is None:
            return None
        where = query.build_where(self.condition)
        sql, params = where.as_sql(compiler, schema_editor.connection)
        return sql % tuple(schema_editor.quote_value(p) for p in params)

    def constraint_sql(self, model, schema_editor):
        query = Query(model, alias_cols=False)
        compiler = query.get_compiler(connection=schema_editor.connection)
        expressions = self._get_expression_sql(compiler, schema_editor.connection, query)
        condition = self._get_condition_sql(compiler, schema_editor, query)
        return self.template % {
            'name': schema_editor.quote_name(self.name),
            'index_type': self.index_type,
            'expressions': ', '.join(expressions),
            'where': ' WHERE (%s)' % condition if condition else '',
            'deferrable': schema_editor._deferrable_constraint_sql(self.deferrable),
        }

    def create_sql(self, model, schema_editor):
        return Statement(
            'ALTER TABLE %(table)s ADD %(constraint)s',
            table=Table(model._meta.db_table, schema_editor.quote_name),
            constraint=self.constraint_sql(model, schema_editor),
        )

    def remove_sql(self, model, schema_editor):
        return schema_editor._delete_constraint_sql(
            schema_editor.sql_delete_check,
            model,
            schema_editor.quote_name(self.name),
        )

    def deconstruct(self):
        path, args, kwargs = super().deconstruct()
        kwargs['expressions'] = self.expressions
        if self.condition is not None:
            kwargs['condition'] = self.condition
        if self.index_type.lower() != 'gist':
            kwargs['index_type'] = self.index_type
        if self.deferrable:
            kwargs['deferrable'] = self.deferrable
        return path, args, kwargs

    def __eq__(self, other):
        if isinstance(other, self.__class__):
            return (
                self.name == other.name and
                self.index_type == other.index_type and
                self.expressions == other.expressions and
                self.condition == other.condition and
                self.deferrable == other.deferrable
            )
        return super().__eq__(other)

    def __repr__(self):
        return '<%s: index_type=%s, expressions=%s%s%s>' % (
            self.__class__.__qualname__,
            self.index_type,
            self.expressions,
            '' if self.condition is None else ', condition=%s' % self.condition,
            '' if self.deferrable is None else ', deferrable=%s' % self.deferrable,
        )
